"""Keyword search over stored document chunks.

Usage:
    python -m neuroscope.search "amyloid related imaging abnormalities"
    python -m neuroscope.search "cognitive decline" --drug lecanemab --limit 5

This is Postgres full-text search (word stems, ranked by term frequency), not
semantic search: it finds the words you type, not their meaning. Every hit
shows its source link and publication date. `--drug` accepts any trusted name
of a drug and keeps only documents linked to it.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import psycopg

from .db import connect
from .normalize import alias_key


@dataclass
class Hit:
    title: str
    url: str | None
    source_type: str
    published_date: str | None
    journal: str | None
    snippet: str
    rank: float
    drugs: list[str]


def resolve_drug(conn: psycopg.Connection, name: str) -> int | None:
    """Drug id for a trusted name (verified alias or canonical name), else None."""
    row = conn.execute(
        "SELECT a.drug_id FROM drug_aliases a JOIN drugs d ON d.id = a.drug_id WHERE a.alias_key = %s "
        "AND (a.review_status = 'verified' OR lower(d.canonical_name) = lower(%s))",
        (alias_key(name), name.strip())).fetchone()
    return row[0] if row else None


def search(conn: psycopg.Connection, query: str, drug: str | None = None, limit: int = 5) -> list[Hit]:
    drug_id = None
    if drug:
        drug_id = resolve_drug(conn, drug)
        if drug_id is None:
            raise ValueError(f"no drug with the trusted name {drug!r}")
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT DISTINCT ON (d.id) d.title, d.url, d.source_type, d.published_date::text, d.journal,
                   ts_headline('english', c.text, q, 'MaxWords=40, MinWords=20, StartSel=**, StopSel=**'),
                   ts_rank(c.tsv, q) AS rank,
                   ARRAY(SELECT dr.canonical_name FROM document_drugs dd JOIN drugs dr ON dr.id = dd.drug_id
                         WHERE dd.document_id = d.id ORDER BY 1)
            FROM chunks c
            JOIN documents d ON d.id = c.document_id,
                 websearch_to_tsquery('english', %(q)s) q
            WHERE c.tsv @@ q
              AND (%(drug)s::int IS NULL OR EXISTS (SELECT 1 FROM document_drugs dd
                                                    WHERE dd.document_id = d.id AND dd.drug_id = %(drug)s))
            ORDER BY d.id, ts_rank(c.tsv, q) DESC
        ) best ORDER BY rank DESC, published_date DESC NULLS LAST LIMIT %(limit)s
        """, {"q": query, "drug": drug_id, "limit": limit}).fetchall()
    return [Hit(*r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query")
    p.add_argument("--drug", help="only documents linked to this drug")
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv)
    with connect() as conn:
        try:
            hits = search(conn, args.query, args.drug, args.limit)
        except ValueError as exc:
            print(exc)
            return 2
    if not hits:
        print("No matching documents.")
        return 0
    for n, h in enumerate(hits, start=1):
        print(f"{n}. {h.title}")
        print(f"   {h.journal or h.source_type}, published {h.published_date or 'date unknown'} | {h.url}")
        print(f"   drugs: {', '.join(h.drugs) or '(none linked)'}")
        print(f"   ...{h.snippet}...\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
