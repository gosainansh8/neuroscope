"""End-to-end ingestion tests against a real Postgres with pgvector.

Set TEST_DATABASE_URL to a database you don't mind wiping, e.g.
    export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/neuroscope_test
Without it these tests skip; with REQUIRE_DB_TESTS=1 a missing database is an error.
All data here is synthetic (fake NCT9xxxxxxx IDs).
"""

from neuroscope.ingest import run_ingestion
from tests.fixtures import all_studies, iv, mutated, study


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


def test_ingestion_tags_trials_with_the_disease_whose_query_returned_them(conn):
    from neuroscope import tools
    both = study("NCT96000001", title="Lecanemab in Parkinson's disease", interventions=[iv("lecanemab")])
    run_ingestion(conn, [both], "cond:Alzheimer Disease", "synthetic", disease="alzheimers")
    run_ingestion(conn, [study("NCT96000002", title="Parkinson study", interventions=[iv("lecanemab")]), both],
                  "cond:Parkinson Disease", "synthetic", disease="parkinsons")
    tags = conn.execute("SELECT nct_id, disease FROM trial_diseases ORDER BY 1, 2").fetchall()
    assert tags == [("NCT96000001", "alzheimers"), ("NCT96000001", "parkinsons"), ("NCT96000002", "parkinsons")]
    # Lookups filter by disease, accept plain names, and refuse unknown ones.
    assert tools.get_trials(conn, "lecanemab", disease="alzheimers")["summary"]["n_trials"] == 1
    assert tools.get_trials(conn, "lecanemab", disease="Parkinson's disease")["summary"]["n_trials"] == 2
    assert tools.get_trials(conn, "lecanemab")["summary"]["n_trials"] == 2
    assert tools.find_drug(conn, "lecanemab")["trials_by_disease"] == {"alzheimers": 1, "parkinsons": 2}
    assert tools.resolve_disease(conn, "ALS") == "als"
    assert tools.resolve_disease(conn, "Lou Gehrig's disease") == "als"
    import pytest
    with pytest.raises(ValueError):
        tools.resolve_disease(conn, "migraine")
    pd = tools.list_pipeline(conn, min_phase=0, active_only=False, disease="parkinsons")
    assert pd["disease_filter"] == "parkinsons" and [d["drug"] for d in pd["drugs"]] == ["lecanemab"]


def test_off_topic_studies_are_skipped_and_pruned(conn):
    from neuroscope.ingest import prune_off_topic
    cancer = study("NCT96000011", title="Pembrolizumab in PD-L1 positive lung cancer", interventions=[iv("pembrolizumab")])
    cancer["protocolSection"]["conditionsModule"] = {"conditions": ["Non-small Cell Lung Cancer"], "keywords": ["PD-L1"]}
    pd = study("NCT96000012", title="Opicapone in Parkinson's disease", interventions=[iv("opicapone")])
    s = run_ingestion(conn, [cancer, pd], "cond:Parkinson Disease", "synthetic", disease="parkinsons")
    assert (s.new, s.off_topic) == (1, 1)
    assert [r[0] for r in conn.execute("SELECT nct_id FROM trials ORDER BY 1")] == ["NCT96000012"]
    # A trial tagged earlier but not about the disease (here: tagged by hand) is pruned with its drugs.
    run_ingestion(conn, [cancer], "cond:Lung", "synthetic")
    conn.execute("INSERT INTO trial_diseases (nct_id, disease) VALUES ('NCT96000011', 'parkinsons')")
    conn.commit()
    assert prune_off_topic(conn) == {"tags_removed": 1, "trials_deleted": 1}
    assert [r[0] for r in conn.execute("SELECT nct_id FROM trials ORDER BY 1")] == ["NCT96000012"]
    assert conn.execute("SELECT COUNT(*) FROM drugs WHERE canonical_name = 'pembrolizumab'").fetchone()[0] == 0
