"""Database connection, schema setup, and seeding.

Usage:
    python -m neuroscope.db init      # create tables and load the curated seed aliases
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "db" / "schema.sql"
SEED_DRUGS = ROOT / "data" / "seed_drugs.csv"
SEED_ALIASES = ROOT / "data" / "seed_aliases.csv"

DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/neuroscope"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())


def init_db(conn: psycopg.Connection) -> dict[str, int]:
    """Apply the schema and load seed drugs/aliases. Idempotent."""
    from .normalize import alias_key, combination_key  # local import keeps db importable standalone

    with conn.transaction():
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

        n_drugs = n_aliases = 0
        with SEED_DRUGS.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = row["canonical_name"].strip()
                components = [c.strip() for c in (row.get("components") or "").split(";") if c.strip()]
                is_combo = len(components) >= 2
                cur = conn.execute(
                    "INSERT INTO drugs (canonical_name, mechanism, modality, is_seeded, is_combination) "
                    "VALUES (%s, %s, %s, TRUE, %s) "
                    "ON CONFLICT (canonical_name) DO UPDATE SET "
                    "  mechanism = EXCLUDED.mechanism, modality = EXCLUDED.modality, "
                    "  is_seeded = TRUE, is_combination = EXCLUDED.is_combination "
                    "RETURNING id",
                    (name, row["mechanism"] or None, row["modality"] or None, is_combo),
                )
                drug_id = cur.fetchone()[0]
                n_drugs += 1
                if is_combo:
                    n_aliases += _seed_alias(conn, combination_key(components), name, drug_id, "combination")
                else:
                    n_aliases += _seed_alias(conn, alias_key(name), name, drug_id, "inn")

        with SEED_ALIASES.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cur = conn.execute("SELECT id FROM drugs WHERE canonical_name = %s",
                                   (row["canonical_name"].strip(),))
                found = cur.fetchone()
                if not found:
                    raise ValueError(f"seed alias references unknown drug: {row['canonical_name']}")
                alias = row["alias"].strip()
                n_aliases += _seed_alias(conn, alias_key(alias), alias, found[0], row["alias_type"])

    return {"seed_drugs": n_drugs, "seed_aliases": n_aliases}


def _seed_alias(conn, key: str, display: str, drug_id: int, alias_type: str) -> int:
    """Seed aliases are verified and win over anything ingestion learned.

    If this re-points an alias that ingestion had attached elsewhere, run
    `python -m neuroscope.curate reresolve` afterwards to re-link trials.
    """
    conn.execute(
        "INSERT INTO drug_aliases (alias_key, alias_display, drug_id, alias_type, source, "
        "review_status, reviewed_at) VALUES (%s, %s, %s, %s, 'seed', 'verified', now()) "
        "ON CONFLICT (alias_key) DO UPDATE SET "
        "  drug_id = EXCLUDED.drug_id, alias_display = EXCLUDED.alias_display, "
        "  alias_type = EXCLUDED.alias_type, source = 'seed', "
        "  review_status = 'verified', reviewed_at = now()",
        (key, display, drug_id, alias_type),
    )
    return 1


def main(argv: list[str]) -> int:
    if argv[:1] != ["init"]:
        print(__doc__)
        return 2
    with connect() as conn:
        counts = init_db(conn)
    print(f"Schema applied. Seeded {counts['seed_drugs']} drugs, {counts['seed_aliases']} aliases.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
