"""The HTTP API. Real Postgres, synthetic data, a scripted stand-in model; no network."""

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from neuroscope import api
from neuroscope.agent import ModelTurn, ToolCall
from tests.test_agent_db import ScriptedModel, seed


@pytest.fixture()
def client(conn, tmp_path):
    seed(conn, tmp_path)
    conn.commit()
    api.app.dependency_overrides[api.get_conn] = lambda: conn
    api.app.dependency_overrides[api.get_embedder] = lambda: None
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def use_model(turns):
    model = ScriptedModel(turns)
    model.model = "scripted"
    api.app.dependency_overrides[api.get_model] = lambda: model
    return model


def test_health_reports_what_the_database_holds(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["trials"] == 3 and body["drugs_with_fda_approval"] == 2
    assert body["document_chunks"] == 1 and body["chunks_with_embeddings"] == 0


def test_ask_returns_answer_lookups_and_verified_citations(client):
    use_model([ModelTurn([ToolCall("get_fda_status", {"drug": "lecanemab"})]),
               ModelTurn(text="Lecanemab is FDA-approved under BLA999001 (first approved 2023-01-06).")])
    r = client.post("/ask", json={"question": "Is lecanemab approved?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"].startswith("Lecanemab is FDA-approved")
    assert body["lookups"] == [{"tool": "get_fda_status", "arguments": {"drug": "lecanemab"}, "error": None}]
    assert body["cited"]["fda_application"] == ["BLA999001"]
    assert body["citations_verified"] is True and body["model"] == "scripted"


def test_ask_reports_an_invented_citation(client):
    use_model([ModelTurn(text="Masitinib was approved under NDA123456.")])
    body = client.post("/ask", json={"question": "Is masitinib approved?"}).json()
    assert body["citations_verified"] is False
    assert body["unsupported_citations"]["fda_application"] == ["NDA123456"]


def test_ask_validates_the_question(client):
    use_model([])
    assert client.post("/ask", json={"question": "a"}).status_code == 422
    assert client.post("/ask", json={"question": "x" * 501}).status_code == 422
    assert client.post("/ask", json={}).status_code == 422


def test_model_failure_is_a_generic_502_and_the_reason_is_logged(client, caplog):
    class Failing:
        model = "failing"

        def start(self, *args):
            raise RuntimeError("Vertex AI request failed (404 NOT_FOUND): projects/demo-123/locations/x")
    api.app.dependency_overrides[api.get_model] = lambda: Failing()
    with caplog.at_level("ERROR", logger="neuroscope.api"):
        r = client.post("/ask", json={"question": "Is lecanemab approved?"})
    assert r.status_code == 502 and r.json()["detail"] == api.MODEL_UNAVAILABLE
    assert "demo-123" not in r.text                       # the project path is not shown to visitors
    assert "projects/demo-123" in caplog.text             # but is kept for whoever runs the service


def test_no_api_documentation_pages_are_published(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_stopped_database_is_a_503_with_instructions(monkeypatch):
    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(api, "connect", refuse)
    with pytest.raises(HTTPException) as err:
        next(api.get_conn())
    assert err.value.status_code == 503 and "deploy/start.sh" in err.value.detail
    with TestClient(api.app) as c:
        assert c.get("/health").status_code == 503


def test_web_page_is_served_and_self_contained(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert 'id="ask-form"' in page and "NeuroScope" in page
    # Nothing is loaded from other sites: no external scripts, styles, fonts, or images.
    # (The favicon is an inline data: URI.)
    import re
    assert "<script src" not in page and "<img" not in page and "@import" not in page and "url(http" not in page
    assert all(href.startswith("data:") for href in re.findall(r'<link[^>]*href="([^"]+)"', page))
    # Model output is escaped before any markup is added, and identifiers link to their sources.
    assert "escapeHtml(s)" in page and "clinicaltrials.gov/study/" in page and "pubmed.ncbi.nlm.nih.gov/" in page


# --- Limits for a public deployment ---------------------------------------------

@pytest.fixture(autouse=True)
def fresh_visitor_counts():
    api._visitor_hits.clear()
    yield
    api._visitor_hits.clear()


def answering_model(text="Lecanemab has one trial here (NCT95000001)."):
    """A model that looks up trials and answers, for any number of questions."""
    class Repeating:
        model, calls = "scripted", 0

        def start(self, *args):
            Repeating.calls += 1
            return ModelTurn([ToolCall("get_trials", {"drug": "lecanemab"})])

        def send_tool_results(self, results):
            return ModelTurn(text=text)
    api.app.dependency_overrides[api.get_model] = lambda: Repeating()
    return Repeating


def test_repeated_question_is_served_from_cache_without_the_model(client, conn):
    model = answering_model()
    first = client.post("/ask", json={"question": "What trials is lecanemab in?"}).json()
    again = client.post("/ask", json={"question": "  what trials is  LECANEMAB in? "}).json()   # same question
    assert (first["cached"], again["cached"]) == (False, True) and again["answer"] == first["answer"]
    assert model.calls == 1
    assert conn.execute("SELECT questions FROM api_usage WHERE day = current_date").fetchone()[0] == 1
    assert client.get("/health").json()["questions_today"] == 1


def test_answers_with_unsupported_citations_are_not_cached(client, conn):
    answering_model("Approved under NDA123456.")
    client.post("/ask", json={"question": "Is it approved?"})
    assert conn.execute("SELECT COUNT(*) FROM api_answer_cache").fetchone()[0] == 0


def test_daily_cap_stops_model_calls_but_cached_answers_still_work(client, monkeypatch):
    monkeypatch.setenv("NEUROSCOPE_DAILY_QUESTION_LIMIT", "2")
    monkeypatch.setenv("NEUROSCOPE_VISITOR_LIMIT", "100")
    model = answering_model()
    assert client.post("/ask", json={"question": "question one"}).status_code == 200
    assert client.post("/ask", json={"question": "question two"}).status_code == 200
    blocked = client.post("/ask", json={"question": "question three"})
    assert blocked.status_code == 429 and "limit for today" in blocked.json()["detail"]
    assert model.calls == 2
    assert client.post("/ask", json={"question": "question one"}).json()["cached"] is True


def test_monthly_cap(client, conn, monkeypatch):
    monkeypatch.setenv("NEUROSCOPE_MONTHLY_QUESTION_LIMIT", "3")
    conn.execute("INSERT INTO api_usage (day, questions) VALUES (date_trunc('month', current_date)::date, 3) "
                 "ON CONFLICT (day) DO UPDATE SET questions = 3")
    conn.commit()
    model = answering_model()
    blocked = client.post("/ask", json={"question": "anything new"})
    assert blocked.status_code == 429 and "for the month" in blocked.json()["detail"] and model.calls == 0


def test_per_visitor_limit_uses_the_real_forwarded_address(client, monkeypatch):
    monkeypatch.setenv("NEUROSCOPE_VISITOR_LIMIT", "2")
    answering_model()
    # The caller controls the first entries of X-Forwarded-For; only the last one counts.
    def post(n, spoofed):
        return client.post("/ask", json={"question": f"question number {n}"},
                           headers={"x-forwarded-for": f"{spoofed}, 203.0.113.9"})
    assert post(1, "1.1.1.1").status_code == 200 and post(2, "2.2.2.2").status_code == 200
    limited = post(3, "3.3.3.3")
    assert limited.status_code == 429 and "last 10 minutes" in limited.json()["detail"]
    # A different visitor is unaffected.
    other = client.post("/ask", json={"question": "question number 4"}, headers={"x-forwarded-for": "198.51.100.7"})
    assert other.status_code == 200


def test_visitor_window_expires():
    api.check_visitor("192.0.2.1", now=0.0)
    for t in (1.0, 2.0, 3.0, 4.0):
        api.check_visitor("192.0.2.1", now=t)
    with pytest.raises(HTTPException):
        api.check_visitor("192.0.2.1", now=5.0)
    api.check_visitor("192.0.2.1", now=700.0)          # the earlier questions have aged out


# --- What changed ---------------------------------------------------------------

def test_changes_lists_new_registrations_status_changes_and_latest_approvals(conn, tmp_path):
    import copy
    from datetime import date, timedelta
    from neuroscope.fda import load
    from neuroscope.ingest import run_ingestion
    from tests.fixtures import iv, study
    from tests.test_fda_db import app as fda_app, export

    def registered(nct, first_posted, status="RECRUITING"):
        raw = study(nct, status=status, interventions=[iv("Lecanemab", "BIOLOGICAL")])
        raw["protocolSection"]["statusModule"]["studyFirstPostDateStruct"] = {"date": first_posted}
        return raw

    recent = (date.today() - timedelta(days=2)).isoformat()
    old_trial = registered("NCT96000001", "2019-05-01")
    run_ingestion(conn, [registered("NCT96000002", recent), old_trial], "first load", "synthetic")
    changed = copy.deepcopy(old_trial)
    changed["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
    run_ingestion(conn, [changed], "refresh", "synthetic")
    from neuroscope.fda import load_labels
    from tests.test_fda_db import label
    load(conn, export(tmp_path, [fda_app("BLA999001", ["LECANEMAB-IRMB"], brand="LEQEMBI"),
                                 fda_app("NDA999002", ["DONEPEZIL HYDROCHLORIDE"], brand="ARICEPT", orig=("AP", "19961125")),
                                 fda_app("NDA999003", ["SEMAGLUTIDE"], brand="NEWBRAND", orig=("AP", "20260101"))]))
    load_labels(conn, iter([
        label("s1", ["BLA999001"], "LEQEMBI is indicated for the treatment of Alzheimer's disease."),
        label("s2", ["NDA999002"], "ARICEPT is indicated for dementia of the Alzheimer's type."),
        label("s3", ["NDA999003"], "NEWBRAND is indicated for type 2 diabetes mellitus.")]))
    conn.commit()
    api.app.dependency_overrides[api.get_conn] = lambda: conn
    try:
        body = TestClient(api.app).get("/changes?days=7").json()
    finally:
        api.app.dependency_overrides.clear()
    # Only the trial first posted in the window is new; the initial load does not make the old one "new".
    assert [(t["nct_id"], t["first_posted"], t["drugs"]) for t in body["new_trials"]] == [("NCT96000002", recent, ["lecanemab"])]
    assert [(c["nct_id"], c["field"], c["old"], c["new"]) for c in body["trial_changes"]] == [
        ("NCT96000001", "overall_status", "RECRUITING", "COMPLETED")]
    # The newest approval (a diabetes product) is left out: its label does not mention Alzheimer's.
    assert [(a["drug"], a["approved"]) for a in body["latest_fda_approvals"]] == [("lecanemab", "2023-01-06"),
                                                                                  ("donepezil", "1996-11-25")]
    assert body["days"] == 7 and body["last_refresh"] is None


def test_follow_up_is_answered_with_history_and_never_cached(client, conn):
    seen = []

    class Recording:
        model, calls = "scripted", 0

        def start(self, system, tool_specs, question, history=None):
            seen.append(history)
            return ModelTurn([ToolCall("get_trials", {"drug": "lecanemab"})])

        def send_tool_results(self, results):
            return ModelTurn(text="Lecanemab is in NCT95000001.")
    api.app.dependency_overrides[api.get_model] = lambda: Recording()
    turn = {"question": "Is lecanemab approved?", "answer": "Yes."}
    for _ in range(2):
        r = client.post("/ask", json={"question": "What trials is it in?", "history": [turn]})
        assert r.status_code == 200 and r.json()["cached"] is False
    assert seen == [[turn], [turn]]
    assert conn.execute("SELECT COUNT(*) FROM api_answer_cache").fetchone()[0] == 0
    assert client.post("/ask", json={"question": "What trials is it in?", "history": [turn] * 4}).status_code == 422


# --- Streaming answers ---------------------------------------------------------------

import json  # noqa: E402
from contextlib import contextmanager  # noqa: E402


class StreamingModel:
    """Looks up trials, then writes its answer in pieces through on_text, as Gemini streams."""
    model = "scripted-stream"

    def __init__(self, pieces):
        self.pieces, self.on_text = pieces, None

    def start(self, *args):
        return ModelTurn([ToolCall("get_trials", {"drug": "lecanemab"})])

    def send_tool_results(self, results):
        for p in self.pieces:
            self.on_text(p)
        return ModelTurn(text="".join(self.pieces))


@pytest.fixture()
def stream_client(client, conn, monkeypatch):
    @contextmanager
    def same_conn():
        yield conn
    monkeypatch.setattr(api, "stream_connection", same_conn)
    return client


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_sends_lookups_then_text_then_the_checked_answer(stream_client, conn):
    model = StreamingModel(["Lecanemab is in ", "NCT95000001", "."])
    api.app.dependency_overrides[api.get_model] = lambda: model
    r = stream_client.post("/ask/stream", json={"question": "What trials is lecanemab in?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    ev = events(r)
    assert [e["type"] for e in ev] == ["lookup", "text", "text", "text", "done"]
    assert ev[0] == {"type": "lookup", "tool": "get_trials", "arguments": {"drug": "lecanemab"}}
    assert "".join(e["delta"] for e in ev if e["type"] == "text") == "Lecanemab is in NCT95000001."
    done = ev[-1]
    assert done["answer"] == "Lecanemab is in NCT95000001." and done["citations_verified"] is True
    assert done["cited"]["nct"] == ["NCT95000001"] and done["cached"] is False
    # The same question again is a single "done" event from the cache.
    again = events(stream_client.post("/ask/stream", json={"question": "What trials is lecanemab in?"}))
    assert [e["type"] for e in again] == ["done"] and again[0]["cached"] is True


def test_stream_reports_model_failure_as_an_error_event(stream_client):
    class Failing:
        model, on_text = "failing", None

        def start(self, *args):
            raise RuntimeError("Vertex AI request failed (504 DEADLINE_EXCEEDED): projects/demo-1/x")
    api.app.dependency_overrides[api.get_model] = lambda: Failing()
    ev = events(stream_client.post("/ask/stream", json={"question": "Is lecanemab approved?"}))
    assert ev == [{"type": "error", "detail": api.MODEL_UNAVAILABLE}]


def test_stream_limits_are_ordinary_errors_before_streaming(stream_client, monkeypatch):
    monkeypatch.setenv("NEUROSCOPE_DAILY_QUESTION_LIMIT", "0")
    api.app.dependency_overrides[api.get_model] = lambda: StreamingModel(["x"])
    r = stream_client.post("/ask/stream", json={"question": "A brand new question"})
    assert r.status_code == 429 and "limit for today" in r.json()["detail"]


def test_report_mode_uses_the_report_prompt_costs_three_and_is_cached_separately(client, conn):
    model = use_model([ModelTurn([ToolCall("get_fda_status", {"drug": "lecanemab"})]),
                       ModelTurn(text="## Summary\nLecanemab is FDA-approved (BLA999001).")])
    body = client.post("/ask", json={"question": "lecanemab", "mode": "report"}).json()
    assert body["answer"].startswith("## Summary") and body["citations_verified"] is True
    assert "REPORT" in model.system and model.question == "Write a report on: lecanemab"
    assert conn.execute("SELECT questions FROM api_usage WHERE day = current_date").fetchone()[0] == 3
    # The same text as an ordinary question is not served the report from the cache.
    use_model([ModelTurn(text="Lecanemab is an antibody.")])
    plain = client.post("/ask", json={"question": "lecanemab"}).json()
    assert plain["answer"] == "Lecanemab is an antibody." and plain["cached"] is False
    assert client.post("/ask", json={"question": "lecanemab", "mode": "report"}).json()["cached"] is True
    assert client.post("/ask", json={"question": "x y z", "mode": "essay"}).status_code == 422
