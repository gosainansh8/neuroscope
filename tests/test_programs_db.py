"""Program status: a drug in a disease is active, discontinued, dormant, or completed, judged from its trials."""

import pytest
from fastapi.testclient import TestClient

from neuroscope import api, tools
from neuroscope.ingest import run_ingestion
from tests.fixtures import iv, mutated, study


def ingest(conn, studies, disease="alzheimers"):
    return run_ingestion(conn, studies, "test", "synthetic", disease=disease)


def test_program_status_is_read_from_the_trials(conn):
    stopped = study("NCT97000001", title="Fakeumab Phase 3", status="TERMINATED", interventions=[iv("fakeumab")])
    stopped["protocolSection"]["statusModule"]["whyStopped"] = "Futility at interim analysis"
    stopped["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2026-08-01"}
    earlier = study("NCT97000002", title="Fakeumab Phase 2", status="COMPLETED", phases=["PHASE2"],
                    interventions=[iv("fakeumab")])
    earlier["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2024-01-01"}
    running = study("NCT97000003", title="Otherumab study", status="RECRUITING", interventions=[iv("otherumab")])
    old = study("NCT97000004", title="Oldinib study", status="COMPLETED", interventions=[iv("oldinib")])
    old["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"] = {"date": "2015-01-01"}
    ingest(conn, [stopped, earlier, running, old])

    fake = tools.get_program_status(conn, "fakeumab")
    p = fake["programs"][0]
    assert (p["program_status"], p["n_trials"], p["n_active_trials"]) == ("discontinued", 2, 0)
    assert (p["latest_closed_trial"], p["why_stopped"]) == ("NCT97000001", "Futility at interim analysis")
    assert tools.get_program_status(conn, "otherumab")["programs"][0]["program_status"] == "active"
    assert tools.get_program_status(conn, "oldinib")["programs"][0]["program_status"] == "dormant"
    listed = tools.get_program_status(conn, disease="Alzheimer's", status="discontinued", min_phase=3)
    assert [x["drug"] for x in listed["programs"]] == ["fakeumab"]
    assert "registry" in listed["caveat"]
    with pytest.raises(ValueError):
        tools.get_program_status(conn, status="paused")


def test_weekly_news_flags_a_stop_that_ended_a_program(conn):
    only = study("NCT97000010", title="Lastumab study", status="RECRUITING", interventions=[iv("lastumab")])
    ingest(conn, [only])
    ingest(conn, [mutated(only, overallStatus="TERMINATED", whyStopped="Sponsor decision")])
    conn.commit()
    api.app.dependency_overrides[api.get_conn] = lambda: conn
    try:
        body = TestClient(api.app).get("/changes?days=7").json()
    finally:
        api.app.dependency_overrides.clear()
    change = [c for c in body["trial_changes"] if c["field"] == "overall_status"][0]
    assert (change["new"], change["ended_programs"]) == ("TERMINATED", ["lastumab"])
