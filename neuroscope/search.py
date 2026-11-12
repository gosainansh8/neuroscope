"""Search over stored document chunks: by keyword, or by meaning.

Usage:
    python -m neuroscope.search "amyloid related imaging abnormalities"
    python -m neuroscope.search "cognitive decline" --drug lecanemab --limit 5
    python -m neuroscope.search "brain swelling side effects" --semantic

Default is Postgres full-text search (word stems, ranked by term frequency):
it finds the words you type, not their meaning. `--semantic` embeds the
question and returns the nearest chunks by meaning; it needs embeddings
(`python -m neuroscope.embed run`) and only sees chunks that have one.
Every hit shows its source link and publication date. `--drug` accepts any
trusted name of a drug and keeps only documents linked to it.
"""

from __future__ import annotations

import argparse
import re
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
    rank: float                    # keyword: term-frequency rank; semantic: cosine similarity (0 to 1)
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


def semantic_search(conn: psycopg.Connection, embedder, query: str, drug: str | None = None,
                    limit: int = 5) -> list[Hit]:
    """Nearest chunks to the question by cosine distance, best chunk per document.
    Only chunks embedded by the embedder's own model are compared."""
    from .embed import to_pgvector
    drug_id = None
    if drug:
        drug_id = resolve_drug(conn, drug)
        if drug_id is None:
            raise ValueError(f"no drug with the trusted name {drug!r}")
    vector = to_pgvector(embedder.embed_query(query))
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT DISTINCT ON (d.id) d.title, d.url, d.source_type, d.published_date::text, d.journal,
                   left(c.text, 300),
                   1 - (c.embedding <=> %(v)s::vector) AS similarity,
                   ARRAY(SELECT dr.canonical_name FROM document_drugs dd JOIN drugs dr ON dr.id = dd.drug_id
                         WHERE dd.document_id = d.id ORDER BY 1)
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL AND c.embedding_model = %(model)s
              AND (%(drug)s::int IS NULL OR EXISTS (SELECT 1 FROM document_drugs dd
                                                    WHERE dd.document_id = d.id AND dd.drug_id = %(drug)s))
            ORDER BY d.id, c.embedding <=> %(v)s::vector
        ) best ORDER BY similarity DESC LIMIT %(limit)s
        """, {"v": vector, "model": embedder.model, "drug": drug_id, "limit": limit}).fetchall()
    return [Hit(*r) for r in rows]


def _any_word_query(query: str) -> str:
    """'lecanemab APOE4 ARIA' -> 'lecanemab or APOE4 or ARIA', so a chunk matching some words still counts."""
    words = re.findall(r"[\w-]+", query)
    return " or ".join(words) if words else query


def hybrid_search(conn: psycopg.Connection, embedder, query: str, drug: str | None = None,
                  limit: int = 5, pool: int = 30) -> list[Hit]:
    """Keyword and meaning-based search combined by reciprocal rank fusion.

    Each method ranks its best `pool` chunks; a chunk scores 1/(60 + rank) for each method that
    found it, so one found by both rises to the top. Keyword search catches exact terms (APOE4,
    a trial code) that meaning-based search can miss; meaning-based search catches paraphrases
    ("brain swelling" for "edema") that keyword search misses. The excerpt is centred on the
    matching words when there are any. Hit.rank is the fused score (higher is better).
    """
    from .embed import to_pgvector
    drug_id = None
    if drug:
        drug_id = resolve_drug(conn, drug)
        if drug_id is None:
            raise ValueError(f"no drug with the trusted name {drug!r}")
    vector = to_pgvector(embedder.embed_query(query))
    rows = conn.execute(
        """
        WITH q AS (SELECT websearch_to_tsquery('english', %(words)s) AS q),
        allowed AS (
            SELECT c.id, c.document_id, c.text, c.tsv, c.embedding, c.embedding_model FROM chunks c
            WHERE %(drug)s::int IS NULL OR EXISTS (SELECT 1 FROM document_drugs dd
                                                   WHERE dd.document_id = c.document_id AND dd.drug_id = %(drug)s)),
        kw AS (
            SELECT id, row_number() OVER (ORDER BY score DESC) AS r FROM (
                SELECT a.id, ts_rank(a.tsv, q.q) AS score FROM allowed a, q WHERE a.tsv @@ q.q
                ORDER BY score DESC LIMIT %(pool)s) k),
        sem AS (
            SELECT id, row_number() OVER (ORDER BY dist) AS r FROM (
                SELECT a.id, a.embedding <=> %(v)s::vector AS dist FROM allowed a
                WHERE a.embedding IS NOT NULL AND a.embedding_model = %(model)s
                ORDER BY dist LIMIT %(pool)s) s),
        fused AS (
            SELECT id, SUM(1.0 / (60 + r)) AS score FROM (SELECT * FROM kw UNION ALL SELECT * FROM sem) x GROUP BY id),
        best AS (
            SELECT DISTINCT ON (a.document_id) a.document_id, a.text, f.score
            FROM fused f JOIN allowed a ON a.id = f.id ORDER BY a.document_id, f.score DESC)
        SELECT d.title, d.url, d.source_type, d.published_date::text, d.journal,
               ts_headline('english', b.text, q.q, 'MaxWords=45, MinWords=20, StartSel=**, StopSel=**'),
               b.score::float,
               ARRAY(SELECT dr.canonical_name FROM document_drugs dd JOIN drugs dr ON dr.id = dd.drug_id
                     WHERE dd.document_id = d.id ORDER BY 1)
        FROM best b JOIN documents d ON d.id = b.document_id, q
        ORDER BY b.score DESC, d.published_date DESC NULLS LAST LIMIT %(limit)s
        """, {"words": _any_word_query(query), "v": vector, "model": embedder.model, "drug": drug_id,
              "pool": pool, "limit": limit}).fetchall()
    return [Hit(*r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query")
    p.add_argument("--drug", help="only documents linked to this drug")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--semantic", action="store_true", help="search by meaning (needs embeddings)")
    p.add_argument("--hybrid", action="store_true", help="keyword and meaning combined (needs embeddings)")
    args = p.parse_args(argv)
    with connect() as conn:
        try:
            if args.hybrid:
                from .embed import VertexEmbedder
                hits = hybrid_search(conn, VertexEmbedder(), args.query, args.drug, args.limit)
            elif args.semantic:
                from .embed import VertexEmbedder
                hits = semantic_search(conn, VertexEmbedder(), args.query, args.drug, args.limit)
            else:
                hits = search(conn, args.query, args.drug, args.limit)
        except (ValueError, RuntimeError) as exc:
            print(exc)
            return 2
    if not hits:
        print("No matching documents.")
        return 0
    for n, h in enumerate(hits, start=1):
        print(f"{n}. {h.title}")
        print(f"   {h.journal or h.source_type}, published {h.published_date or 'date unknown'} | {h.url}")
        print(f"   drugs: {', '.join(h.drugs) or '(none linked)'}")
        if args.semantic:
            print(f"   similarity {h.rank:.2f}")
        print(f"   ...{h.snippet}...\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
