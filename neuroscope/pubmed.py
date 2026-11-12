"""PubMed abstracts for the drugs in the database (NCBI E-utilities, no API key).

Usage:
    python -m neuroscope.pubmed load                         # drugs with 5+ trials, 20 abstracts each
    python -m neuroscope.pubmed load --min-trials 3 --per-drug 30 --save-raw data/raw/pubmed/ad.xml
    python -m neuroscope.pubmed load --from-xml data/raw/pubmed/ad.xml   # replay offline

For each drug, PubMed is searched for abstracts that name the drug and
Alzheimer's in the title or abstract. Each abstract becomes one document
(title, journal, date, PMID link) and one or more text chunks.

HOW A DOCUMENT IS LINKED TO A DRUG: a trusted name of the drug (a verified
alias or its own canonical name) appears in the title or abstract as a whole
word. Being returned by a search is not enough, and unreviewed aliases are
never used. The name that was found is recorded in document_drugs.

Only abstracts are stored (PubMed does not carry full text). Chunks have no
embeddings yet; search them with `python -m neuroscope.search`.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Iterator

import psycopg
import requests

from .ctgov import _session
from .db import connect
from .normalize import parse_partial_date

log = logging.getLogger("neuroscope.pubmed")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL = "neuroscope"
PAUSE_S = 0.4                  # NCBI allows 3 requests per second without a key
CHUNK_WORDS = 220
CHUNK_OVERLAP = 40

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


@dataclass
class Abstract:
    pmid: str
    title: str
    journal: str | None
    published_date: date | None
    published_date_precision: str | None
    text: str                              # abstract sections joined, labels kept ("METHODS: ...")


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _pub_date(pubdate) -> tuple[date | None, str | None]:
    if pubdate is None:
        return None, None
    year, month, day = (pubdate.findtext(t) for t in ("Year", "Month", "Day"))
    if not year:                           # "2019 Nov-Dec", "2021 Spring"
        m = re.match(r"(\d{4})(?:\s+([A-Za-z]{3}))?", pubdate.findtext("MedlineDate") or "")
        if not m:
            return None, None
        year, month, day = m.group(1), m.group(2), None
    if month and not month.isdigit():
        month = str(_MONTHS[month[:3].lower()]) if month[:3].lower() in _MONTHS else None
    parts = [year] + ([f"{int(month):02d}"] if month else []) + ([f"{int(day):02d}"] if month and day else [])
    return parse_partial_date("-".join(parts))


def parse_articles(xml_text: str) -> Iterator[Abstract]:
    """Yield abstracts from a PubmedArticleSet. Articles without a PMID or abstract are skipped."""
    root = ET.fromstring(xml_text)
    for art in root.iter("PubmedArticle"):
        cit = art.find("MedlineCitation")
        if cit is None:
            continue
        pmid = (cit.findtext("PMID") or "").strip()
        article = cit.find("Article")
        if not pmid or article is None:
            continue
        sections = []
        for sec in article.findall("Abstract/AbstractText"):
            body = _text(sec)
            if body:
                label = sec.get("Label")
                sections.append(f"{label}: {body}" if label else body)
        if not sections:
            continue
        published, precision = _pub_date(article.find("Journal/JournalIssue/PubDate"))
        yield Abstract(pmid, _text(article.find("ArticleTitle")) or f"PMID {pmid}",
                       article.findtext("Journal/Title"), published, precision, "\n".join(sections))


def chunk_text(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split into overlapping word windows. Most abstracts fit in one or two chunks."""
    words = text.split()
    if len(words) <= size:
        return [" ".join(words)] if words else []
    chunks, start = [], 0
    while start < len(words):
        chunks.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
        start += size - overlap
    return chunks


def search_term(canonical_name: str) -> str:
    """'[18F]florbetapir' -> 'florbetapir': PubMed indexes the name without the label."""
    return re.sub(r"^\[[^\]]*\]", "", canonical_name).strip()


def name_patterns(conn: psycopg.Connection) -> list[tuple[int, str, re.Pattern]]:
    """(drug_id, name, whole-word pattern) for every trusted single-drug name of 4+ characters."""
    from .resolver import _own_key
    rows = conn.execute(
        "SELECT a.alias_key, a.alias_display, a.review_status, d.id, d.canonical_name FROM drug_aliases a "
        "JOIN drugs d ON d.id = a.drug_id WHERE a.review_status <> 'rejected' AND NOT d.is_combination "
        "AND NOT d.is_class").fetchall()                  # a class name in a paper is not about one drug
    out = []
    for key, display, status, drug_id, canonical in rows:
        if status != "verified" and key != _own_key(canonical):
            continue
        name = search_term(display)
        if len(re.sub(r"[^A-Za-z0-9]", "", name)) < 4:
            continue                       # "CBD", "PiB": too short to match safely in free text
        flexible = r"[\s\-]?".join(re.escape(p) for p in re.split(r"[\s\-]+", name) if p)
        out.append((drug_id, display, re.compile(rf"(?<![A-Za-z0-9]){flexible}(?![A-Za-z0-9])", re.IGNORECASE)))
    return out


def fetch_pmids(session, term: str, per_drug: int) -> list[str]:
    query = f'"{term}"[Title/Abstract] AND alzheimer*[Title/Abstract]'
    resp = session.get(f"{EUTILS}/esearch.fcgi", timeout=30, params={
        "db": "pubmed", "term": query, "retmax": per_drug, "retmode": "json", "sort": "relevance", "tool": TOOL})
    resp.raise_for_status()
    return resp.json()["esearchresult"].get("idlist", [])


def fetch_xml(session, pmids: list[str]) -> str:
    resp = session.post(f"{EUTILS}/efetch.fcgi", timeout=120, data={
        "db": "pubmed", "id": ",".join(pmids), "retmode": "xml", "tool": TOOL})
    resp.raise_for_status()
    return resp.text


def store(conn: psycopg.Connection, abstracts: Iterator[Abstract]) -> dict[str, int]:
    """Insert new abstracts, re-chunk changed ones, and (re)link every stored abstract to drugs."""
    patterns = name_patterns(conn)
    summary = {"abstracts": 0, "new": 0, "changed": 0, "unchanged": 0, "duplicates": 0, "chunks": 0,
               "drug_links": 0}
    with conn.transaction():
        for ab in abstracts:
            summary["abstracts"] += 1
            digest = hashlib.sha256(f"{ab.title}\n{ab.text}".encode("utf-8")).hexdigest()
            row = conn.execute("SELECT id, content_hash FROM documents WHERE source_type = 'pubmed' "
                               "AND external_id = %s", (ab.pmid,)).fetchone()
            if row and row[1] == digest:
                doc_id = row[0]
                conn.execute("UPDATE documents SET retrieved_at = now() WHERE id = %s", (doc_id,))
                summary["unchanged"] += 1
            elif conn.execute("SELECT 1 FROM documents WHERE content_hash = %s", (digest,)).fetchone():
                # The same title and abstract under another PMID (a duplicate PubMed record): keep one.
                summary["duplicates"] += 1
                continue
            else:
                fields = (ab.title, f"https://pubmed.ncbi.nlm.nih.gov/{ab.pmid}/", ab.published_date,
                          ab.published_date_precision, ab.journal, digest)
                if row:
                    doc_id = row[0]
                    conn.execute("UPDATE documents SET title = %s, url = %s, published_date = %s, "
                                 "published_date_precision = %s, journal = %s, content_hash = %s, "
                                 "retrieved_at = now() WHERE id = %s", fields + (doc_id,))
                    conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
                    summary["changed"] += 1
                else:
                    doc_id = conn.execute(
                        "INSERT INTO documents (source_type, external_id, title, url, published_date, "
                        "published_date_precision, journal, content_hash) "
                        "VALUES ('pubmed', %s, %s, %s, %s, %s, %s, %s) RETURNING id", (ab.pmid,) + fields).fetchone()[0]
                    summary["new"] += 1
                for i, chunk in enumerate(chunk_text(ab.text)):
                    conn.execute("INSERT INTO chunks (document_id, chunk_index, text, token_count) "
                                 "VALUES (%s, %s, %s, %s)", (doc_id, i, chunk, len(chunk.split())))
                    summary["chunks"] += 1
            # Links are recomputed every time so they follow alias corrections.
            summary["drug_links"] += _link(conn, doc_id, f"{ab.title}\n{ab.text}", patterns)
    return summary


def _link(conn: psycopg.Connection, doc_id: int, haystack: str, patterns) -> int:
    conn.execute("DELETE FROM document_drugs WHERE document_id = %s", (doc_id,))
    linked: set[int] = set()
    for drug_id, name, pattern in patterns:
        if drug_id not in linked and pattern.search(haystack):
            linked.add(drug_id)
            conn.execute("INSERT INTO document_drugs (document_id, drug_id, matched_name) VALUES (%s, %s, %s)",
                         (doc_id, drug_id, name))
    return len(linked)


def relink(conn: psycopg.Connection) -> dict[str, int]:
    """Recompute every stored document's drug links from its stored text, after alias corrections."""
    patterns = name_patterns(conn)
    docs = conn.execute(
        "SELECT d.id, d.title || E'\n' || COALESCE(string_agg(c.text, E'\n' ORDER BY c.chunk_index), '') "
        "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id GROUP BY d.id").fetchall()
    with conn.transaction():
        links = sum(_link(conn, doc_id, text, patterns) for doc_id, text in docs)
    return {"documents": len(docs), "drug_links": links}


def iter_live(conn: psycopg.Connection, min_trials: int, per_drug: int, max_drugs: int | None,
              raw_parts: list[str] | None = None, skipped: list[str] | None = None) -> Iterator[Abstract]:
    """Abstracts for each drug. A drug whose requests still fail after retries is skipped
    (and named in `skipped`) so one bad response does not lose every other drug."""
    drugs = [r[0] for r in conn.execute(
        "SELECT canonical_name FROM v_drug_pipeline WHERE n_trials >= %s AND NOT canonical_name LIKE '%% + %%' "
        "ORDER BY n_trials DESC, canonical_name", (min_trials,))][:max_drugs]
    session, seen = _session(("GET", "POST")), set()     # efetch is a read-only POST, safe to retry
    for n, name in enumerate(drugs, start=1):
        try:
            pmids = [p for p in fetch_pmids(session, search_term(name), per_drug) if p not in seen]
            time.sleep(PAUSE_S)
            if not pmids:
                log.info("%d/%d %s: no new abstracts", n, len(drugs), name)
                continue
            xml_text = fetch_xml(session, pmids)
            time.sleep(PAUSE_S)
        except requests.RequestException as exc:
            log.warning("%d/%d %s: skipped after retries: %s", n, len(drugs), name, exc)
            if skipped is not None:
                skipped.append(name)
            continue
        seen.update(pmids)
        log.info("%d/%d %s: %d new abstracts", n, len(drugs), name, len(pmids))
        if raw_parts is not None:
            raw_parts.append(xml_text)
        yield from parse_articles(xml_text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("load")
    sp.add_argument("--min-trials", type=int, default=5, help="only drugs with at least this many trials")
    sp.add_argument("--per-drug", type=int, default=20, help="abstracts per drug, most relevant first")
    sp.add_argument("--max-drugs", type=int, default=None)
    sp.add_argument("--save-raw", metavar="PATH", help="write the fetched PubMed XML here")
    sp.add_argument("--from-xml", metavar="PATH", help="replay a file written by --save-raw")
    sub.add_parser("relink", help="recompute drug links for every stored document")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.cmd == "relink":
        with connect() as conn:
            r = relink(conn)
        print(f"Relinked {r['documents']} documents: {r['drug_links']} drug links")
        return 0
    with connect() as conn:
        if args.from_xml:
            with open(args.from_xml, encoding="utf-8") as fh:
                parts = [part for part in fh.read().split("\n<!--neuroscope-part-->\n") if part.strip()]
            abstracts = [a for part in parts for a in parse_articles(part)]
        else:
            raw_parts: list[str] | None = [] if args.save_raw else None
            abstracts = list(iter_live(conn, args.min_trials, args.per_drug, args.max_drugs, raw_parts))
            if args.save_raw:
                with open(args.save_raw, "w", encoding="utf-8") as fh:
                    fh.write("\n<!--neuroscope-part-->\n".join(raw_parts or []))
        s = store(conn, iter(abstracts))
    print(f"PubMed: {s['abstracts']} abstracts ({s['new']} new, {s['changed']} changed, {s['unchanged']} "
          f"unchanged, {s['duplicates']} duplicates skipped), {s['chunks']} chunks written, {s['drug_links']} drug links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
