"""A small HTTP API around the agent, for deployment.

Run locally:
    pip install -e ".[vertex,api]"
    uvicorn neuroscope.api:app --port 8080
    curl -s localhost:8080/health
    curl -s -X POST localhost:8080/ask -H 'content-type: application/json' \\
         -d '{"question": "Is semaglutide approved for Alzheimer'"'"'s disease?"}'

Endpoints:
    GET  /         a single web page for asking questions (neuroscope/static/index.html)
    GET  /health   is the database reachable, and how much data it holds
    POST /ask      {"question": "..."} -> the agent's answer, its lookups, and the citation check

The API has no login of its own. On Cloud Run it is deployed so that only
signed-in callers the project allows can reach it (see deploy/README.md);
each question costs a model call, so it should never be left open to the public.

If the database is stopped, both endpoints answer 503 with a message saying
so, rather than failing obscurely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .agent import GeminiModel, answer_question, build_tools
from .db import connect

app = FastAPI(title="NeuroScope", description="Sourced answers about Alzheimer's drug development.")

INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

DATABASE_DOWN = ("The database is not reachable. If it was stopped to save cost, start it with "
                 "deploy/start.sh and try again in a minute.")


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class Lookup(BaseModel):
    tool: str
    arguments: dict
    error: str | None = None


class Answer(BaseModel):
    answer: str
    lookups: list[Lookup]
    cited: dict[str, list[str]]
    unsupported_citations: dict[str, list[str]]
    citations_verified: bool
    stopped_early: bool
    model: str | None = None


def get_conn() -> Iterator[psycopg.Connection]:
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        raise HTTPException(status_code=503, detail=DATABASE_DOWN) from exc
    try:
        yield conn
    finally:
        conn.close()


def get_model():
    """The chat model for one request. Tests replace this with a scripted model."""
    try:
        return GeminiModel()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def get_embedder():
    """The embedder for literature search. None means keyword search."""
    from .embed import VertexEmbedder
    try:
        return VertexEmbedder()
    except RuntimeError:
        return None


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    """The web page. It is one self-contained file: no external scripts, fonts, or styles."""
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/health")
def health(conn: psycopg.Connection = Depends(get_conn)) -> dict:
    trials, drugs, fda, chunks, embedded, latest = conn.execute(
        "SELECT (SELECT COUNT(*) FROM trials), (SELECT COUNT(*) FROM drugs), "
        "(SELECT COUNT(*) FROM v_drug_fda_approval), (SELECT COUNT(*) FROM chunks), "
        "(SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL), "
        "(SELECT MAX(last_retrieved_at)::date::text FROM trials)").fetchone()
    return {"status": "ok", "trials": trials, "drugs": drugs, "drugs_with_fda_approval": fda,
            "document_chunks": chunks, "chunks_with_embeddings": embedded, "trials_last_retrieved": latest}


@app.post("/ask", response_model=Answer)
def ask(body: Question, conn: psycopg.Connection = Depends(get_conn), model=Depends(get_model),
        embedder=Depends(get_embedder)) -> Answer:
    try:
        result = answer_question(body.question, model, build_tools(conn, embedder))
    except RuntimeError as exc:                      # the model service failed or timed out
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Answer(
        answer=result.answer,
        lookups=[Lookup(tool=c.name, arguments=c.args, error=r.get("error")) for c, r in result.steps],
        cited=result.cited, unsupported_citations=result.unsupported,
        citations_verified=not any(result.unsupported.values()),
        stopped_early=result.stopped_early, model=getattr(model, "model", None))
