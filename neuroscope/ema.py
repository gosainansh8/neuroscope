"""EU marketing authorisations from the European Medicines Agency (EMA).

Usage:
    python -m neuroscope.ema load                                   # download the current file, then load it
    python -m neuroscope.ema load --file data/raw/ema/medicines.json

SOURCE: the EMA's public medicines file (JSON, refreshed twice a day), one record
per medicine the EMA has evaluated: authorised, refused, withdrawn, or under
evaluation. Only human medicines are used.

WHAT THIS DOES NOT SAY: the file covers only medicines authorised CENTRALLY, for
the whole EU at once. Many older drugs (donepezil, galantamine) were authorised
country by country and are not in it. A drug with no EMA record is therefore not
shown to be unauthorised in Europe; say "no centrally authorised EU medicine".

HOW A MEDICINE IS MATCHED TO A DRUG: the same rules as FDA products
(fda.match_product). The medicine's international non-proprietary name, then its
active substance, must exactly equal a TRUSTED name of a drug (a verified alias or
the drug's own canonical name), as published or with a salt word or radiolabel
notation normalised. Several substances (separated by ';') match only a
combination drug with exactly those substances. Unreviewed aliases never match.

`indication_mentions_alzheimers` is a plain text search of the indication and
therapeutic area, a pointer worth reading, not a regulatory determination.
Loading replaces all EMA rows, so re-run it after correcting aliases.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg
import requests
from psycopg.types.json import Jsonb

from .ctgov import USER_AGENT
from .db import ROOT, connect
from .fda import FdaProduct, match_product, trusted_keys

log = logging.getLogger("neuroscope.ema")

DOWNLOAD_URL = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json"
RAW_DIR = ROOT / "data" / "raw" / "ema"
_ALZHEIMER_RE = re.compile(r"alzheimer", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _date(value: str | None) -> date | None:
    """EMA dates are DD/MM/YYYY; empty means none."""
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _text(value: str | None) -> str | None:
    if not value:
        return None
    clean = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", value))).strip()
    return clean or None


def _substances(value: str | None) -> list[str]:
    return [s.strip() for s in html.unescape(value or "").replace("\xa0", " ").split(";") if s.strip()]


def match_medicine(raw: dict, trusted: dict[str, int]) -> tuple[int, str, str] | None:
    """(drug_id, matched key, method) for one EMA record, or None. INN first, then active substance."""
    for field in ("international_non_proprietary_name_common_name", "active_substance"):
        names = _substances(raw.get(field))
        if names:
            hit = match_product(FdaProduct("", None, names, None, None, None), trusted)
            if hit:
                return hit
    return None


def read_file(path: str | Path) -> tuple[datetime, list[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    stamp = (data.get("meta") or {}).get("timestamp")
    if not stamp:
        raise ValueError(f"{path}: file has no meta.timestamp")
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")), data.get("data") or []


def download(dest_dir: Path = RAW_DIR) -> Path:
    resp = requests.get(DOWNLOAD_URL, headers={"User-Agent": USER_AGENT}, timeout=120)
    resp.raise_for_status()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"medicines_{date.today().isoformat()}.json"
    dest.write_bytes(resp.content)
    return dest


def load(conn: psycopg.Connection, path: str | Path) -> dict[str, int]:
    """Replace all EMA rows with the matched human medicines from one file. One transaction."""
    stamp, records = read_file(path)
    summary = {"medicines_read": 0, "medicines_stored": 0, "authorised": 0, "drugs_matched": 0}
    drugs: set[int] = set()
    with conn.transaction():
        trusted = trusted_keys(conn)
        conn.execute("DELETE FROM ema_medicines")
        for raw in records:
            if raw.get("category") != "Human" or not raw.get("ema_product_number"):
                continue
            summary["medicines_read"] += 1
            hit = match_medicine(raw, trusted)
            if not hit:
                continue
            drug_id, key, method = hit
            indication = _text(raw.get("therapeutic_indication"))
            area = _text(raw.get("therapeutic_area_mesh"))
            conn.execute(
                "INSERT INTO ema_medicines (ema_product_number, name, medicine_status, inn, active_substance, "
                "therapeutic_area, therapeutic_indication, indication_mentions_alzheimers, "
                "marketing_authorisation_holder, authorisation_date, refusal_date, application_withdrawal_date, "
                "withdrawal_date, conditional_approval, generic, biosimilar, url, drug_id, matched_key, "
                "match_method, source_timestamp, raw) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (ema_product_number) DO NOTHING",
                (raw["ema_product_number"].strip(), _text(raw.get("name_of_medicine")) or raw["ema_product_number"],
                 raw.get("medicine_status") or "Unknown",
                 _text(raw.get("international_non_proprietary_name_common_name")),
                 _text(raw.get("active_substance")), area, indication,
                 bool(_ALZHEIMER_RE.search(f"{indication or ''} {area or ''}")),
                 _text(raw.get("marketing_authorisation_developer_applicant_holder")),
                 _date(raw.get("marketing_authorisation_date")),
                 _date(raw.get("refusal_of_marketing_authorisation_date")),
                 _date(raw.get("withdrawal_of_application_date")),
                 _date(raw.get("withdrawal_expiry_revocation_lapse_of_marketing_authorisation_date")),
                 raw.get("conditional_approval") == "Yes", raw.get("generic") == "Yes",
                 raw.get("biosimilar") == "Yes", raw.get("medicine_url") or None,
                 drug_id, key, method, stamp, Jsonb(raw)))
            summary["medicines_stored"] += 1
            summary["authorised"] += raw.get("medicine_status") == "Authorised"
            drugs.add(drug_id)
    summary["drugs_matched"] = len(drugs)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("load")
    sp.add_argument("--file", help="a saved EMA medicines JSON file; downloaded if omitted")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    path = args.file or download()
    with connect() as conn:
        s = load(conn, path)
    print(f"EMA file {path}: {s['medicines_read']} human medicines read, {s['medicines_stored']} matched "
          f"to {s['drugs_matched']} drugs ({s['authorised']} currently authorised)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
