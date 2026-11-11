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


def test_model_failure_is_a_502_with_the_reason(client):
    class Failing:
        model = "failing"

        def start(self, *args):
            raise RuntimeError("Vertex AI request failed (504 DEADLINE_EXCEEDED): Deadline expired")
    api.app.dependency_overrides[api.get_model] = lambda: Failing()
    r = client.post("/ask", json={"question": "Is lecanemab approved?"})
    assert r.status_code == 502 and "DEADLINE_EXCEEDED" in r.json()["detail"]


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
    assert '<form id="ask-form">' in page and "NeuroScope" in page
    # Nothing is loaded from other sites: no external scripts, styles, fonts, or images.
    assert "<script src" not in page and "<link" not in page and "<img" not in page and "@import" not in page
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
