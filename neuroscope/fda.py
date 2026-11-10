"""FDA approval records from Drugs@FDA (the openFDA bulk export).

Usage:
    python -m neuroscope.fda load                      # download the current export, then load it
    python -m neuroscope.fda load --file data/raw/fda/drugsfda_2026-11-06.json.zip
    python -m neuroscope.fda labels --save-raw data/raw/fda/labels.jsonl   # indications, live API
    python -m neuroscope.fda labels --from-jsonl data/raw/fda/labels.jsonl # replay offline

WHAT COUNTS AS APPROVED: an application whose ORIGINAL submission has status
'AP'. A tentative approval ('TA') is not an approval. Nothing here is ever
inferred from trial phase or status.

WHAT THIS DOES NOT SAY: Drugs@FDA records that a product was approved, not
what it was approved for. A match here means "FDA-approved for at least one
use", never "approved for Alzheimer's disease".

HOW AN FDA PRODUCT IS MATCHED TO A DRUG (the complete list):
  Exact key match between the product's active ingredient and a TRUSTED name
  of a drug: a verified alias, or the drug's own canonical name. Unreviewed
  aliases never match. The ingredient is tried as published, then with three
  documented FDA naming conventions removed:
    - the four-letter biologic suffix        LECANEMAB-IRMB      -> lecanemab
    - a radiolabel, in the project's notation FLORBETAPIR F-18    -> [18F]florbetapir
    - a trailing salt or counter-ion          DONEPEZIL HYDROCHLORIDE -> donepezil
  Esters (acetate, propionate) are not stripped: they can be different drugs.
  A product with several active ingredients matches only a combination drug
  with exactly those ingredients, never one of its ingredients alone.

Only applications with at least one matched product are stored. Loading
replaces all FDA rows, so re-run it after correcting aliases.

INDICATIONS come from FDA drug labels (openFDA label API), fetched for the
approved NDA and BLA applications stored by `load`. When a drug has no
marketed NDA/BLA product (galantamine's brand, Razadyne, is discontinued),
the labels of its approved generic (ANDA) applications are used instead. The label's "Indications
and Usage" text is kept verbatim. `indications_mention_alzheimers` is a plain
text search for "alzheimer" in that section. It is a pointer to labels worth
reading, not a regulatory determination: a label can mention the disease in a
limitation of use. Read the stored text before relying on it.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator

import psycopg
import requests
from psycopg.types.json import Jsonb

from .ctgov import USER_AGENT
from .db import ROOT, connect
from .normalize import alias_key, clean_drug_name, combination_key, split_combination

log = logging.getLogger("neuroscope.fda")

DOWNLOAD_INDEX = "https://api.fda.gov/download.json"
LABEL_URL = "https://api.fda.gov/drug/label.json"
_ALZHEIMER_RE = re.compile(r"alzheimer", re.IGNORECASE)
RAW_DIR = ROOT / "data" / "raw" / "fda"

# FDA nonproprietary names for biologics end in a hyphen and four meaningless letters.
_BIOLOGIC_SUFFIX_RE = re.compile(r"-[A-Za-z]{4}$")

# Salts and counter-ions only. Esters are deliberately absent.
_SALT_WORDS = {
    "hydrochloride", "dihydrochloride", "hydrobromide", "chloride", "bromide", "sulfate",
    "maleate", "fumarate", "besylate", "tartrate", "bitartrate", "succinate", "citrate",
    "mesylate", "dimesylate", "oxalate", "tosylate", "phosphate", "sodium", "disodium",
    "potassium", "calcium", "magnesium", "hyclate", "lactate", "gluconate", "saccharate",
    "aspartate", "tromethamine", "carbonate",
}


@dataclass
class FdaProduct:
    product_number: str
    brand_name: str | None
    active_ingredients: list[str]            # exactly as published
    dosage_form: str | None
    route: str | None
    marketing_status: str | None


@dataclass
class FdaApplication:
    application_number: str
    application_type: str                    # NDA | BLA | ANDA
    sponsor_name: str | None
    approval_status: str                     # approved | tentative | no_submission_record
    original_approval_date: date | None
    products: list[FdaProduct] = field(default_factory=list)


def _parse_date(value: str | None) -> date | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def parse_application(raw: dict) -> FdaApplication:
    number = (raw.get("application_number") or "").strip()
    m = re.match(r"(NDA|BLA|ANDA)\d+$", number)
    if not m:
        raise ValueError(f"unrecognized application number: {number!r}")

    originals = [s for s in raw.get("submissions") or [] if s.get("submission_type") == "ORIG"]
    approved = sorted(d for d in (_parse_date(s.get("submission_status_date")) for s in originals
                                  if s.get("submission_status") == "AP") if d)
    if any(s.get("submission_status") == "AP" for s in originals):
        status = "approved"
    elif any(s.get("submission_status") == "TA" for s in originals):
        status = "tentative"
    else:
        status = "no_submission_record"

    products = [
        FdaProduct(
            product_number=str(p.get("product_number") or ""),
            brand_name=(p.get("brand_name") or "").strip() or None,
            active_ingredients=[i["name"].strip() for i in p.get("active_ingredients") or [] if i.get("name")],
            dosage_form=p.get("dosage_form"),
            route=p.get("route"),
            marketing_status=p.get("marketing_status"),
        )
        for p in raw.get("products") or []
    ]
    return FdaApplication(number, m.group(1), raw.get("sponsor_name"), status,
                          approved[0] if approved else None, products)


def _strip_salt(name: str) -> str:
    words = name.split()
    while len(words) > 1 and words[-1].lower() in _SALT_WORDS:
        words.pop()
    return " ".join(words)


def ingredient_forms(name: str) -> list[str]:
    """The ingredient as published, then with each FDA convention removed. Most exact first."""
    forms = [name]
    step = _BIOLOGIC_SUFFIX_RE.sub("", name)
    forms.append(step)
    labeled = clean_drug_name(step)[0] or step        # radiolabel into project notation, dose words out
    forms.append(labeled)
    forms.append(clean_drug_name(_strip_salt(step))[0] or _strip_salt(step))
    # A bare salt word ("SODIUM CHLORIDE" -> "CHLORIDE") is not a name.
    return list(dict.fromkeys(f for f in forms if f and f.lower() not in _SALT_WORDS))


def match_product(product: FdaProduct, trusted: dict[str, int]) -> tuple[int, str, str] | None:
    """Return (drug_id, matched key, method) or None. `trusted` maps alias keys to drug ids."""
    names = product.active_ingredients
    if not names:
        return None
    if len(names) == 1:
        for n, form in enumerate(ingredient_forms(names[0])):
            if split_combination(form):
                continue                              # one ingredient is never a combination
            key = alias_key(form)
            if key in trusted:
                return trusted[key], key, "exact" if n == 0 else "normalized"
        return None
    # Several ingredients: only a combination drug with exactly these ingredients.
    for exact in (True, False):
        parts = [n if exact else (ingredient_forms(n) or [n])[-1] for n in names]
        key = combination_key(parts)
        if key in trusted:
            return trusted[key], key, "exact" if exact else "normalized"
    return None


def trusted_keys(conn: psycopg.Connection) -> dict[str, int]:
    """Alias keys allowed to match FDA products: verified aliases and each drug's own name."""
    from .resolver import _own_key
    out: dict[str, int] = {}
    rows = conn.execute(
        "SELECT a.alias_key, a.drug_id, a.review_status, d.canonical_name FROM drug_aliases a "
        "JOIN drugs d ON d.id = a.drug_id WHERE a.review_status <> 'rejected'").fetchall()
    for key, drug_id, status, canonical in rows:
        if status == "verified" or key == _own_key(canonical):
            out[key] = drug_id
    return out


def iter_applications(path: str | Path) -> tuple[date | None, Iterator[dict]]:
    """Read a Drugs@FDA export (.json or .json.zip). Returns (export date, raw applications)."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as fh:
                data = json.load(fh)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    exported = (data.get("meta") or {}).get("last_updated")
    try:
        export_date = date.fromisoformat(exported) if exported else None
    except ValueError:
        export_date = None
    return export_date, iter(data.get("results") or [])


def download(dest_dir: Path = RAW_DIR) -> Path:
    """Download the current Drugs@FDA export. Returns the saved path."""
    headers = {"User-Agent": USER_AGENT}
    index = requests.get(DOWNLOAD_INDEX, headers=headers, timeout=30)
    index.raise_for_status()
    entry = index.json()["results"]["drug"]["drugsfda"]
    parts = entry["partitions"]
    if len(parts) != 1:
        raise RuntimeError(f"expected one Drugs@FDA file, found {len(parts)}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"drugsfda_{entry['export_date']}.json.zip"
    resp = requests.get(parts[0]["file"], headers=headers, timeout=120)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def load(conn: psycopg.Connection, path: str | Path) -> dict[str, int]:
    """Replace all FDA rows with the matched applications from one export. One transaction."""
    export_date, raws = iter_applications(path)
    if export_date is None:
        raise ValueError(f"{path}: export has no meta.last_updated date")
    summary = {"applications_read": 0, "applications_stored": 0, "products_stored": 0,
               "drugs_matched": 0, "unparseable": 0}
    matched_drugs: set[int] = set()
    with conn.transaction():
        trusted = trusted_keys(conn)
        conn.execute("DELETE FROM fda_applications")          # products cascade
        for raw in raws:
            summary["applications_read"] += 1
            try:
                app = parse_application(raw)
            except ValueError as exc:
                summary["unparseable"] += 1
                log.warning("skipped FDA application: %s", exc)
                continue
            matches = [(p, m) for p in app.products if (m := match_product(p, trusted))]
            if not matches:
                continue
            conn.execute(
                "INSERT INTO fda_applications (application_number, application_type, sponsor_name, "
                "approval_status, original_approval_date, source_export_date, raw) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (app.application_number, app.application_type, app.sponsor_name, app.approval_status,
                 app.original_approval_date, export_date, Jsonb(raw)))
            summary["applications_stored"] += 1
            for p, (drug_id, key, method) in matches:
                conn.execute(
                    "INSERT INTO fda_products (application_number, product_number, brand_name, "
                    "active_ingredients, dosage_form, route, marketing_status, drug_id, matched_key, "
                    "match_method) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (app.application_number, p.product_number, p.brand_name, p.active_ingredients,
                     p.dosage_form, p.route, p.marketing_status, drug_id, key, method))
                summary["products_stored"] += 1
                matched_drugs.add(drug_id)
    summary["drugs_matched"] = len(matched_drugs)
    return summary


@dataclass
class FdaLabel:
    set_id: str
    application_numbers: list[str]
    brand_names: list[str]
    effective_date: date | None
    indications_and_usage: str | None        # verbatim; None when the label has no such section
    indications_mention_alzheimers: bool


def parse_label(raw: dict) -> FdaLabel:
    set_id = raw.get("set_id")
    if not set_id:
        raise ValueError("label has no set_id")
    openfda = raw.get("openfda") or {}
    sections = raw.get("indications_and_usage") or []
    text = "\n".join(s for s in sections if s).strip() or None
    return FdaLabel(set_id, openfda.get("application_number") or [], openfda.get("brand_name") or [],
                    _parse_date(raw.get("effective_time")), text, bool(text and _ALZHEIMER_RE.search(text)))


def iter_labels(application_numbers: list[str], batch_size: int = 20, pause_s: float = 0.3) -> Iterator[dict]:
    """Yield raw label records for these applications from the openFDA label API."""
    from .ctgov import _session
    import time
    session = _session()
    numbers = sorted(set(application_numbers))
    for i in range(0, len(numbers), batch_size):
        query = "openfda.application_number:(" + " OR ".join(f'"{n}"' for n in numbers[i:i + batch_size]) + ")"
        skip = 0
        while True:
            resp = session.get(LABEL_URL, params={"search": query, "limit": 100, "skip": skip}, timeout=60)
            if resp.status_code == 404:          # openFDA answers "no matches" with 404
                break
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("results") or []
            yield from results
            skip += len(results)
            if not results or skip >= payload["meta"]["results"]["total"]:
                break
            time.sleep(pause_s)
        time.sleep(pause_s)


def labelled_application_numbers(conn: psycopg.Connection) -> list[str]:
    """Applications whose labels are fetched: approved NDAs and BLAs, plus approved
    ANDAs of drugs that have no marketed NDA/BLA product (their brand label is gone)."""
    return [r[0] for r in conn.execute("""
        SELECT application_number FROM fda_applications
        WHERE approval_status = 'approved' AND application_type IN ('NDA', 'BLA')
        UNION
        SELECT a.application_number FROM fda_applications a
        JOIN fda_products p ON p.application_number = a.application_number
        WHERE a.approval_status = 'approved' AND a.application_type = 'ANDA' AND p.drug_id IN (
            SELECT p2.drug_id FROM fda_products p2
            JOIN fda_applications a2 ON a2.application_number = p2.application_number
            WHERE a2.approval_status = 'approved'
            GROUP BY p2.drug_id
            HAVING NOT BOOL_OR(a2.application_type IN ('NDA', 'BLA')
                               AND p2.marketing_status IN ('Prescription', 'Over-the-counter')))
        ORDER BY 1""")]


def load_labels(conn: psycopg.Connection, raws: Iterator[dict]) -> dict[str, int]:
    """Replace all stored labels. Keeps only labels tied to a stored application."""
    known = set(labelled_application_numbers(conn))
    summary = {"labels_read": 0, "labels_stored": 0, "mention_alzheimers": 0, "unparseable": 0}
    with conn.transaction():
        conn.execute("DELETE FROM fda_labels")
        for raw in raws:
            summary["labels_read"] += 1
            try:
                label = parse_label(raw)
            except ValueError:
                summary["unparseable"] += 1
                continue
            numbers = [n for n in label.application_numbers if n in known]
            if not numbers:
                continue
            n = conn.execute(
                "INSERT INTO fda_labels (set_id, application_numbers, brand_names, effective_date, "
                "indications_and_usage, indications_mention_alzheimers) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (set_id) DO NOTHING",
                (label.set_id, numbers, label.brand_names, label.effective_date,
                 label.indications_and_usage, label.indications_mention_alzheimers)).rowcount
            summary["labels_stored"] += n
            summary["mention_alzheimers"] += int(bool(n) and label.indications_mention_alzheimers)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("load")
    sp.add_argument("--file", help="a saved Drugs@FDA export; downloaded if omitted")
    sp = sub.add_parser("labels")
    sp.add_argument("--save-raw", metavar="PATH", help="write raw label JSON lines here")
    sp.add_argument("--from-jsonl", metavar="PATH", help="replay labels from a saved file")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if args.cmd == "labels":
        with connect() as conn:
            if args.from_jsonl:
                with open(args.from_jsonl, encoding="utf-8") as fh:
                    raws = [json.loads(line) for line in fh if line.strip()]
            else:
                raws = list(iter_labels(labelled_application_numbers(conn)))
                if args.save_raw:
                    with open(args.save_raw, "w", encoding="utf-8") as fh:
                        fh.writelines(json.dumps(r) + "\n" for r in raws)
            s = load_labels(conn, iter(raws))
        print(f"FDA labels: read {s['labels_read']}, stored {s['labels_stored']}; "
              f"{s['mention_alzheimers']} mention Alzheimer's in Indications and Usage")
        return 0
    path = args.file or download()
    with connect() as conn:
        s = load(conn, path)
    print(f"Drugs@FDA export {path}: read {s['applications_read']} applications, stored "
          f"{s['applications_stored']} with {s['products_stored']} products for {s['drugs_matched']} drugs "
          f"({s['unparseable']} unparseable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
