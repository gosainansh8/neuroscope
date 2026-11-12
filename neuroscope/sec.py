"""Company disclosures from SEC EDGAR: press releases (8-K/6-K exhibits) and annual reports (10-K/20-F).

Usage:
    python -m neuroscope.sec load                     # every company in data/sec_companies.csv
    python -m neuroscope.sec load --tickers LLY,BIIB --years 1

SEC_USER_AGENT must be set (in .env): the SEC asks automated clients to identify
themselves with a contact, e.g. "NeuroScope research someone@example.com". It is
sent to sec.gov only. EDGAR allows 10 requests a second; this stays well under.

WHICH COMPANIES: data/sec_companies.csv lists public companies with Alzheimer's,
Parkinson's, or ALS programs, by ticker. Each ticker is turned into the SEC's company
number (CIK) using the SEC's own ticker file, so no company number is guessed.

WHAT IS KEPT: for each company, the press release exhibit (EX-99.1) of every 8-K
with item 2.02, 7.01, or 8.01 (results, Regulation FD, other events) and of every 6-K
in the last few years, plus the latest 10-K or 20-F. A document is stored only if a
trusted name of one of our drugs appears in it, and only the passages that name a
drug (plus the opening passage, for context) are kept. Links to drugs follow the
same whole-word, trusted-name rule as PubMed abstracts. A company's statements are
its own claims: answers attribute them ("Biogen said ...").
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import logging
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import psycopg
import requests

from .ctgov import _session
from .db import ROOT, connect
from .pubmed import _link, chunk_text, name_patterns

log = logging.getLogger("neuroscope.sec")

COMPANIES = ROOT / "data" / "sec_companies.csv"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
PAUSE_S = 0.2
PRESS_ITEMS = ("2.02", "7.01", "8.01")
_TAG_RE = re.compile(r"<(script|style|ix:header)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
# Generic substances and words that are registered as trial interventions somewhere but say nothing
# about a company's pipeline when they appear in a filing.
GENERIC = {"insulin", "glucose", "saline", "water", "oxygen", "vehicle", "placebo", "multiple", "optional",
           "biological", "intrathecal", "standard", "control", "vitamin", "caffeine", "alcohol", "nicotine",
           "sodium", "potassium", "calcium", "magnesium", "iron", "zinc", "protein", "albumin", "human albumin",
           "immunoglobulin", "gene therapy", "stem cells", "stem cell", "cell therapy", "exercise", "diet", "lithium", "melatonin", "sugar"}


def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        raise RuntimeError('Set SEC_USER_AGENT in .env, e.g. SEC_USER_AGENT="NeuroScope research you@example.com"')
    return ua


def sec_session(ua: str | None = None) -> requests.Session:
    s = _session()
    s.headers["User-Agent"] = ua or user_agent()
    return s


def html_to_text(raw: str) -> str:
    text = _TAG_RE.sub("\n", raw)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def headline(text: str, company: str, form: str, filed: str) -> str:
    """The first line that reads like a headline, else a plain label."""
    for line in text.splitlines()[:25]:
        if 25 <= len(line) <= 250 and not re.match(r"(?i)(exhibit|ex-?99|press release$|for immediate release|https?:)", line):
            return line
    return f"{company} {form} filed {filed}"


def companies(tickers: list[str] | None = None) -> list[tuple[str, str]]:
    with COMPANIES.open(encoding="utf-8") as fh:
        rows = [(r["ticker"].strip().upper(), r["company"].strip()) for r in csv.DictReader(fh)]
    return [r for r in rows if not tickers or r[0] in tickers]


def cik_by_ticker(session) -> dict[str, int]:
    resp = session.get(TICKERS_URL, timeout=60)
    resp.raise_for_status()
    return {v["ticker"].upper(): int(v["cik_str"]) for v in resp.json().values()}


def filings(session, cik: int, since: date) -> list[dict]:
    """Press-release filings since `since`, and the latest annual report."""
    resp = session.get(SUBMISSIONS_URL.format(cik=cik), timeout=60)
    resp.raise_for_status()
    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    rows = [dict(zip(recent, values)) for values in zip(*recent.values())] if recent else []
    out, annual = [], None
    for r in rows:
        filed = date.fromisoformat(r["filingDate"])
        if r["form"] in ("10-K", "20-F") and annual is None:
            annual = r
        if filed < since:
            continue
        if r["form"] == "8-K" and any(i in (r.get("items") or "") for i in PRESS_ITEMS):
            out.append(r)
        elif r["form"] == "6-K":
            out.append(r)
    return out + ([annual] if annual else [])


def document_url(session, cik: int, filing: dict) -> str | None:
    """The press release exhibit of an 8-K/6-K, or the main document of an annual report."""
    acc = filing["accessionNumber"].replace("-", "")
    base = ARCHIVE_URL.format(cik=cik, acc=acc)
    if filing["form"] in ("10-K", "20-F"):
        return f"{base}/{filing['primaryDocument']}"
    resp = session.get(f"{base}/index.json", timeout=60)
    resp.raise_for_status()
    names = [i["name"] for i in resp.json().get("directory", {}).get("item", [])]
    exhibits = sorted(n for n in names if re.search(r"(?i)ex[-_]?99", n) and n.lower().endswith((".htm", ".html", ".txt")))
    return f"{base}/{exhibits[0]}" if exhibits else None


def disclosure_patterns(conn: psycopg.Connection) -> list:
    """Stricter than for abstracts: only drugs with a Phase 2+ program or an approval, never a generic
    substance, and a name matched as written (a hyphen is optional only next to a digit, as in codes),
    so "SAM-e" does not match "same"."""
    keep = {r[0] for r in conn.execute(
        "SELECT DISTINCT drug_id FROM v_program_status WHERE COALESCE(highest_phase, 0) >= 2 OR fda_approved "
        "OR ema_authorised")}
    out = []
    # Combination drugs are left out of abstract matching (a paper naming both ingredients is not about the
    # product), but a filing names them by brand or code: AMX0035, Relyvrio, Cobenfy.
    combo_names = conn.execute(
        "SELECT d.id, a.alias_display FROM drug_aliases a JOIN drugs d ON d.id = a.drug_id "
        "WHERE d.is_combination AND a.review_status = 'verified' AND a.alias_type <> 'combination' "
        "AND a.alias_display !~ '[+/]'").fetchall()
    for drug_id, display, _ in name_patterns(conn) + [(i, n, None) for i, n in combo_names]:
        name = re.sub(r"^\[[^\]]*\]", "", display).strip()
        if drug_id not in keep or name.lower() in GENERIC or len(re.sub(r"[^A-Za-z0-9]", "", name)) < 5:
            continue
        parts = re.split(r"([\s-]+)", name)
        rx = ""
        for i, part in enumerate(parts):
            if i % 2:                                   # a separator
                near_digit = parts[i - 1][-1:].isdigit() or (i + 1 < len(parts) and parts[i + 1][:1].isdigit())
                rx += r"[\s-]?" if near_digit else re.escape(part)
            else:
                rx += re.escape(part)
        out.append((drug_id, display, re.compile(rf"(?<![A-Za-z0-9]){rx}(?![A-Za-z0-9])", re.IGNORECASE)))
    return out


def keep_passages(text: str, patterns) -> list[str]:
    """The opening passage and every passage that names a tracked drug."""
    chunks = chunk_text(text)
    return [c for i, c in enumerate(chunks) if i == 0 or any(rx.search(c) for _, _, rx in patterns)]


def store(conn: psycopg.Connection, company: str, filing: dict, url: str, text: str, patterns) -> str:
    """'new', 'unchanged', or 'no_drug'. One document per filing."""
    if not any(rx.search(text) for _, _, rx in patterns):
        return "no_drug"
    kind = "sec_filing" if filing["form"] in ("10-K", "20-F") else "press_release"
    acc = filing["accessionNumber"]
    passages = keep_passages(text, patterns)
    digest = hashlib.sha256("\n".join(passages).encode("utf-8")).hexdigest()
    if conn.execute("SELECT 1 FROM documents WHERE source_type = %s AND external_id = %s", (kind, acc)).fetchone():
        return "unchanged"
    if conn.execute("SELECT 1 FROM documents WHERE content_hash = %s", (digest,)).fetchone():
        return "unchanged"                        # the same release filed twice
    with conn.transaction():
        doc_id = conn.execute(
            "INSERT INTO documents (source_type, external_id, title, url, published_date, published_date_precision, "
            "journal, content_hash) VALUES (%s, %s, %s, %s, %s, 'day', %s, %s) RETURNING id",
            (kind, acc, (f"{company} annual report (Form {filing['form']}), filed {filing['filingDate']}"
                         if kind == "sec_filing" else headline(text, company, filing["form"], filing["filingDate"]))[:500], url,
             filing["filingDate"], f"{company} (SEC Form {filing['form']})", digest)).fetchone()[0]
        for i, chunk in enumerate(passages):
            conn.execute("INSERT INTO chunks (document_id, chunk_index, text, token_count) VALUES (%s, %s, %s, %s)",
                         (doc_id, i, chunk, len(chunk.split())))
        _link(conn, doc_id, "\n".join(passages), patterns)
    return "new"


def load(conn: psycopg.Connection, tickers: list[str] | None = None, years: int = 3, session=None) -> dict:
    session = session or sec_session()
    patterns = disclosure_patterns(conn)
    conn.commit()
    ciks = cik_by_ticker(session)
    since = date.today() - timedelta(days=365 * years)
    summary = {"companies": 0, "filings_read": 0, "new": 0, "unchanged": 0, "no_drug": 0, "no_exhibit": 0,
               "unknown_tickers": [], "errors": 0}
    for ticker, company in companies(tickers):
        cik = ciks.get(ticker)
        if not cik:
            summary["unknown_tickers"].append(ticker)
            continue
        summary["companies"] += 1
        try:
            todo = filings(session, cik, since)
        except requests.RequestException as exc:
            summary["errors"] += 1
            log.warning("%s: submissions failed: %s", ticker, exc)
            continue
        time.sleep(PAUSE_S)
        known = {r[0] for r in conn.execute(
            "SELECT external_id FROM documents WHERE source_type IN ('press_release', 'sec_filing') "
            "AND external_id = ANY(%s)", ([f["accessionNumber"] for f in todo],))}
        conn.commit()
        for f in todo:
            if f["accessionNumber"] in known:           # stored on an earlier run: nothing to fetch
                summary["unchanged"] += 1
                continue
            try:
                url = document_url(session, cik, f)
                time.sleep(PAUSE_S)
                if not url:
                    summary["no_exhibit"] += 1
                    continue
                resp = session.get(url, timeout=120)
                resp.raise_for_status()
                time.sleep(PAUSE_S)
            except requests.RequestException as exc:
                summary["errors"] += 1
                log.warning("%s %s: skipped: %s", ticker, f["accessionNumber"], exc)
                continue
            summary["filings_read"] += 1
            summary[store(conn, company, f, url, html_to_text(resp.text), patterns)] += 1
        log.info("%s: %d filings checked", ticker, len(todo))
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("load")
    sp.add_argument("--tickers", help="comma-separated; default: every company in data/sec_companies.csv")
    sp.add_argument("--years", type=int, default=3, help="press releases from this many years back")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        with connect() as conn:
            s = load(conn, [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None, args.years)
    except RuntimeError as exc:
        print(exc)
        return 2
    print(f"SEC: {s['companies']} companies, {s['filings_read']} filings read; {s['new']} new documents, "
          f"{s['unchanged']} already stored, {s['no_drug']} naming none of our drugs, {s['no_exhibit']} without a "
          f"press release exhibit, {s['errors']} errors" + (f"; unknown tickers: {', '.join(s['unknown_tickers'])}"
                                                           if s["unknown_tickers"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
