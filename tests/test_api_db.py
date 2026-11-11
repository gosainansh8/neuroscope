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
