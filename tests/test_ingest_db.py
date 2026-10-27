"""End-to-end ingestion tests against a real Postgres with pgvector.

Set TEST_DATABASE_URL to a database you don't mind wiping, e.g.
    export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/neuroscope_test
Without it these tests skip; with REQUIRE_DB_TESTS=1 a missing database is an error.
All data here is synthetic (fake NCT9xxxxxxx IDs).
"""

from neuroscope.ingest import run_ingestion
from tests.fixtures import all_studies, mutated


def ingest(conn, studies):
    return run_ingestion(conn, studies, "test", "synthetic")


def drug_of(conn, nct_id):
    rows = conn.execute(
        "SELECT d.canonical_name FROM trial_interventions ti JOIN drugs d ON d.id = ti.drug_id "
        "WHERE ti.nct_id = %s ORDER BY 1", (nct_id,)).fetchall()
    return [r[0] for r in rows]


def test_first_run_counts(conn):
    s = ingest(conn, all_studies())
    assert (s.fetched, s.new, s.changed, s.unchanged, s.failed) == (8, 7, 0, 0, 1)
    run = conn.execute("SELECT status, n_failed FROM ingestion_runs WHERE id = %s", (s.run_id,)).fetchone()
    assert run == ("succeeded", 1)


def test_alias_resolution(conn):
    ingest(conn, all_studies())
    assert drug_of(conn, "NCT90000001") == ["lecanemab"]          # dose stripped, code otherName
    assert drug_of(conn, "NCT90000002") == ["donanemab"]          # parenthetical alias
    assert drug_of(conn, "NCT90000008") == ["donepezil"]          # salt form stripped
    assert drug_of(conn, "NCT90000003") == ["fakeumab"]           # new drug named from otherName
    assert drug_of(conn, "NCT90000006") == []                     # behavioral: no drug
    # 'XYZ1234' == 'XYZ-1234', but that alias is unreviewed and came from another
    # trial, so NCT90000004 waits for review instead of linking.
    assert drug_of(conn, "NCT90000004") == []
    assert conn.execute("SELECT resolution_method, matched_alias_key FROM trial_interventions "
                        "WHERE nct_id = 'NCT90000004'").fetchone() == ("pending_review", "xyz1234")


def test_placebo_is_flagged_and_unlinked(conn):
    ingest(conn, all_studies())
    rows = conn.execute(
        "SELECT name, is_placebo, drug_id FROM trial_interventions WHERE nct_id = 'NCT90000001' ORDER BY name"
    ).fetchall()
    assert ("Placebo", True, None) in rows


def test_conflict_logged_not_merged(conn):
    ingest(conn, all_studies())
    assert drug_of(conn, "NCT90000005") == ["lecanemab"]
    conflicts = conn.execute(
        "SELECT a.canonical_name, b.canonical_name FROM drug_alias_conflicts c "
        "JOIN drugs a ON a.id = c.assigned_drug_id JOIN drugs b ON b.id = c.conflicting_drug_id"
    ).fetchall()
    assert conflicts == [("lecanemab", "aducanumab")]
    # aducanumab's alias still points at aducanumab
    owner = conn.execute(
        "SELECT d.canonical_name FROM drug_aliases a JOIN drugs d ON d.id = a.drug_id "
        "WHERE a.alias_key = 'aducanumab'").fetchone()[0]
    assert owner == "aducanumab"


def test_rerun_is_idempotent(conn):
    ingest(conn, all_studies())
    n_drugs = conn.execute("SELECT COUNT(*) FROM drugs").fetchone()[0]
    s2 = ingest(conn, all_studies())
    assert (s2.new, s2.changed, s2.unchanged) == (0, 0, 7)
    assert conn.execute("SELECT COUNT(*) FROM drugs").fetchone()[0] == n_drugs
    assert conn.execute("SELECT COUNT(*) FROM trial_snapshots").fetchone()[0] == 7


def test_change_tracking(conn):
    studies = all_studies()
    ingest(conn, studies)

    changed = mutated(studies[0], overallStatus="ACTIVE_NOT_RECRUITING")
    changed["protocolSection"]["designModule"]["enrollmentInfo"] = {"count": 1795, "type": "ACTUAL"}
    s2 = ingest(conn, [changed] + studies[1:])
    assert (s2.new, s2.changed) == (0, 1)

    rows = conn.execute(
        "SELECT field, old_value, new_value FROM trial_changes "
        "WHERE run_id = %s ORDER BY field", (s2.run_id,)).fetchall()
    assert rows == [
        ("enrollment", "100", "1795"),
        ("enrollment_type", "ESTIMATED", "ACTUAL"),
        ("overall_status", "RECRUITING", "ACTIVE_NOT_RECRUITING"),
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM trial_snapshots WHERE nct_id = 'NCT90000001'").fetchone()[0] == 2


def test_pipeline_view(conn):
    ingest(conn, all_studies())
    row = conn.execute(
        "SELECT n_trials, n_active_trials, highest_phase_any FROM v_drug_pipeline "
        "WHERE canonical_name = 'lecanemab'").fetchone()
    assert row == (2, 2, 3)
