"""Upgrading an existing database with `db init` keeps every record. Synthetic data."""

from neuroscope.db import init_db
from neuroscope.ingest import run_ingestion
from tests.fixtures import all_studies, iv, study

# The resolution_method constraint as created by the first schema, before migrations.
ORIGINAL_CHECK = (
    "ALTER TABLE trial_interventions DROP CONSTRAINT trial_interventions_resolution_method_check; "
    "ALTER TABLE trial_interventions ADD CONSTRAINT trial_interventions_resolution_method_check "
    "CHECK (resolution_method IN ('matched', 'created', 'conflict', 'placebo', 'non_drug', 'no_usable_name'))"
)

SNAPSHOT_SQL = {
    "trials": "SELECT nct_id, content_hash, data_origin, first_ingested_at FROM trials ORDER BY 1",
    "interventions": "SELECT id, nct_id, name, other_names, drug_id, resolution_method, matched_alias_key "
                     "FROM trial_interventions ORDER BY id",
    "drugs": "SELECT id, canonical_name, is_seeded FROM drugs ORDER BY id",
    "aliases": "SELECT alias_key, drug_id, source, review_status, first_seen_nct, reviewed_at "
               "FROM drug_aliases ORDER BY 1",
    "snapshots": "SELECT nct_id, content_hash FROM trial_snapshots ORDER BY 1, 2",
    "changes": "SELECT id, nct_id, field FROM trial_changes ORDER BY id",
    "runs": "SELECT id, status, n_fetched FROM ingestion_runs ORDER BY id",
}


def snapshot(conn):
    return {k: conn.execute(sql).fetchall() for k, sql in SNAPSHOT_SQL.items()}


def test_init_upgrades_existing_database_without_losing_records(conn, monkeypatch):
    # Recreate a database from before pending_review existed, filled the old way.
    conn.execute(ORIGINAL_CHECK)
    conn.commit()
    monkeypatch.setenv("NEUROSCOPE_TRUST_UNREVIEWED_ALIASES", "1")
    run_ingestion(conn, all_studies(), "before upgrade", "synthetic")
    monkeypatch.delenv("NEUROSCOPE_TRUST_UNREVIEWED_ALIASES")
    before = snapshot(conn)
    assert len(before["trials"]) == 7

    init_db(conn)
    init_db(conn)                       # idempotent: a second run changes nothing either
    assert snapshot(conn) == before

    # The upgraded database accepts the new values.
    run_ingestion(conn, [study("NCT92000001", interventions=[iv("XYZ1234")]),
                         study("NCT92000002", interventions=[iv("Donepezil or Rivastigmine")])],
                  "after upgrade", "synthetic")
    methods = conn.execute("SELECT nct_id, resolution_method FROM trial_interventions "
                           "WHERE nct_id LIKE 'NCT92%' ORDER BY 1").fetchall()
    assert methods == [("NCT92000001", "pending_review"), ("NCT92000002", "ambiguous_name")]
