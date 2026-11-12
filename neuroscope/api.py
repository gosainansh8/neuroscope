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
    GET  /changes  newly registered trials, trial status changes, latest FDA approvals, last refresh
    POST /ask      {"question": "..."} -> the agent's answer, its lookups, and the citation check

The API has no login. Every question that reaches the model costs money, so
/ask is protected by limits that hold even when the service is public:
  - a daily and a monthly cap on questions sent to the model, counted in the
    database (NEUROSCOPE_DAILY_QUESTION_LIMIT, NEUROSCOPE_MONTHLY_QUESTION_LIMIT)
  - a per-visitor limit (NEUROSCOPE_VISITOR_LIMIT questions per 10 minutes)
  - a 24-hour answer cache: a repeated question is answered without the model
When a limit is reached the answer is HTTP 429 with a plain explanation.
The caps bound the cost; they do not stop one visitor from using up the day.

If the database is stopped, both endpoints answer 503 with a message saying
so, rather than failing obscurely.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Iterator

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .agent import GeminiModel, answer_question, build_tools
from .db import connect

# No generated API documentation pages: visitors only need the website.
app = FastAPI(title="NeuroScope", description="Sourced answers about Alzheimer's drug development.",
              docs_url=None, redoc_url=None, openapi_url=None)
log = logging.getLogger("neuroscope.api")

# Shown to visitors instead of the model service's raw error, which can name the project.
# The full error goes to the server log (Cloud Run: Logging in the console).
MODEL_UNAVAILABLE = ("The answer service is temporarily unavailable. Please try again in a minute.")

INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

VISITOR_WINDOW_S = 600
CACHE_HOURS = 24


def _limit(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


class Limits:
    """Read from the environment on each request, so they can be changed without a rebuild."""

    @property
    def daily(self) -> int:
        return _limit("NEUROSCOPE_DAILY_QUESTION_LIMIT", 50)

    @property
    def monthly(self) -> int:
        return _limit("NEUROSCOPE_MONTHLY_QUESTION_LIMIT", 500)

    @property
    def per_visitor(self) -> int:
        return _limit("NEUROSCOPE_VISITOR_LIMIT", 5)


limits = Limits()
_visitor_hits: dict[str, deque] = defaultdict(deque)
_visitor_lock = threading.Lock()


def client_ip(request: Request) -> str:
    """The caller's address. Behind Cloud Run the real address is the LAST entry of
    X-Forwarded-For; earlier entries are whatever the caller chose to send."""
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    return parts[-1] if parts else (request.client.host if request.client else "unknown")


def check_visitor(ip: str, now: float | None = None) -> None:
    """Allow at most `per_visitor` model questions per address per window. Kept in memory:
    the service runs as a single instance, and the database caps are the real ceiling."""
    now = time.monotonic() if now is None else now
    with _visitor_lock:
        hits = _visitor_hits[ip]
        while hits and now - hits[0] > VISITOR_WINDOW_S:
            hits.popleft()
        if len(hits) >= limits.per_visitor:
            raise HTTPException(status_code=429, detail=(
                f"You have asked {limits.per_visitor} questions in the last 10 minutes. "
                "Please wait a few minutes and try again."))
        hits.append(now)


def reserve_question(conn: psycopg.Connection) -> None:
    """Count one question against the caps, or refuse it. One atomic statement per check,
    so two requests at once cannot both take the last slot."""
    month = conn.execute("SELECT COALESCE(SUM(questions), 0) FROM api_usage "
                         "WHERE day >= date_trunc('month', current_date)").fetchone()[0]
    if month >= limits.monthly:
        conn.commit()
        raise HTTPException(status_code=429, detail=(
            "This demo has reached its question limit for the month. Example questions that were "
            "asked recently still work. Please come back next month."))
    row = conn.execute(
        "INSERT INTO api_usage (day, questions) VALUES (current_date, 1) "
        "ON CONFLICT (day) DO UPDATE SET questions = api_usage.questions + 1 "
        "WHERE api_usage.questions < %s RETURNING questions", (limits.daily,)).fetchone()
    conn.commit()
    if row is None:
        raise HTTPException(status_code=429, detail=(
            "This demo has reached its question limit for today. Example questions that were "
            "asked recently still work. Please come back tomorrow."))


def question_key(question: str) -> str:
    normalized = re.sub(r"\s+", " ", question.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

DATABASE_DOWN = ("The database is not reachable. If it was stopped to save cost, start it with "
                 "deploy/start.sh and try again in a minute.")


class Turn(BaseModel):
    """An earlier question and answer in the same conversation, sent back by the page."""
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=6000)


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=3)


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
    cached: bool = False                 # served from a recent identical question; no model call
    attribution_warnings: list[dict] = []    # a cited record that belongs to a different drug than the sentence names


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
        log.error("model setup failed: %s", exc)
        raise HTTPException(status_code=503, detail=MODEL_UNAVAILABLE) from exc


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
    today = conn.execute("SELECT COALESCE((SELECT questions FROM api_usage WHERE day = current_date), 0)"
                         ).fetchone()[0]
    return {"status": "ok", "trials": trials, "drugs": drugs, "drugs_with_fda_approval": fda,
            "document_chunks": chunks, "chunks_with_embeddings": embedded, "trials_last_retrieved": latest,
            "questions_today": today, "daily_question_limit": limits.daily}


CHANGE_FIELDS = ("overall_status", "phase_label", "has_results", "why_stopped", "primary_completion_date")


@app.get("/changes")
def changes(days: int = 7, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    """What is new: trials first posted on ClinicalTrials.gov in the window (from each record's own
    first-posted date, so an initial load does not count as "new"), field changes detected by
    refreshes in the window, the latest FDA approvals whose label indications mention Alzheimer's
    (an application for an unrelated use of a tracked drug is left out), and the last refresh."""
    days = max(1, min(days, 90))
    new_trials = conn.execute(
        """
        SELECT t.nct_id, t.brief_title, t.phase_label, t.overall_status,
               s.raw #>> '{protocolSection,statusModule,studyFirstPostDateStruct,date}' AS first_posted,
               ARRAY(SELECT DISTINCT d.canonical_name FROM trial_interventions ti JOIN drugs d ON d.id = ti.drug_id
                     WHERE ti.nct_id = t.nct_id ORDER BY 1)
        FROM trials t JOIN trial_snapshots s ON s.nct_id = t.nct_id AND s.content_hash = t.content_hash
        WHERE (s.raw #>> '{protocolSection,statusModule,studyFirstPostDateStruct,date}') >= to_char(current_date - %s, 'YYYY-MM-DD')
        ORDER BY first_posted DESC, t.nct_id LIMIT 25
        """, (days,)).fetchall()
    field_changes = conn.execute(
        """
        SELECT c.nct_id, t.brief_title, c.field, c.old_value, c.new_value, c.detected_at::date::text
        FROM trial_changes c JOIN trials t ON t.nct_id = c.nct_id
        WHERE c.field = ANY(%s) AND c.detected_at >= now() - make_interval(days => %s)
        ORDER BY c.detected_at DESC, c.nct_id LIMIT 40
        """, (list(CHANGE_FIELDS), days)).fetchall()
    approvals = conn.execute(
        """
        SELECT DISTINCT ON (a.application_number) a.application_number, d.canonical_name, a.original_approval_date::text,
               p.brand_name, a.sponsor_name
        FROM fda_applications a JOIN fda_products p ON p.application_number = a.application_number
        JOIN drugs d ON d.id = p.drug_id
        WHERE a.approval_status = 'approved' AND a.application_type IN ('NDA', 'BLA') AND a.original_approval_date IS NOT NULL
          AND EXISTS (SELECT 1 FROM fda_labels l WHERE a.application_number = ANY (l.application_numbers)
                      AND l.indications_mention_alzheimers)
        ORDER BY a.application_number, p.product_number
        """).fetchall()
    approvals = sorted(approvals, key=lambda r: r[2], reverse=True)[:6]
    last = conn.execute("SELECT finished_at::timestamp(0)::text, status FROM refresh_runs "
                        "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone() \
        if conn.execute("SELECT to_regclass('refresh_runs')").fetchone()[0] else None
    return {
        "days": days,
        "new_trials": [dict(zip(["nct_id", "title", "phase", "status", "first_posted", "drugs"], r)) for r in new_trials],
        "trial_changes": [dict(zip(["nct_id", "title", "field", "old", "new", "detected"], r)) for r in field_changes],
        "latest_fda_approvals": [dict(zip(["application_number", "drug", "approved", "brand", "sponsor"], r))
                                 for r in approvals],
        "last_refresh": {"finished_at": last[0], "status": last[1]} if last else None,
        "trials_last_retrieved": conn.execute("SELECT MAX(last_retrieved_at)::date::text FROM trials").fetchone()[0],
    }


@app.post("/ask", response_model=Answer)
def ask(body: Question, request: Request, conn: psycopg.Connection = Depends(get_conn),
        model=Depends(get_model), embedder=Depends(get_embedder)) -> Answer:
    key = question_key(body.question)
    follow_up = bool(body.history)        # a follow-up depends on earlier turns, so it is never cached
    cached = None if follow_up else conn.execute(
        "SELECT response FROM api_answer_cache WHERE question_key = %s "
        "AND created_at > now() - make_interval(hours => %s)", (key, CACHE_HOURS)).fetchone()
    conn.commit()
    if cached:
        return Answer(**{**cached[0], "cached": True})

    check_visitor(client_ip(request))
    reserve_question(conn)
    try:
        result = answer_question(body.question, model, build_tools(conn, embedder),
                                 history=[t.model_dump() for t in body.history])
    except RuntimeError as exc:                      # the model service failed or timed out
        log.error("model request failed: %s", exc)
        raise HTTPException(status_code=502, detail=MODEL_UNAVAILABLE) from exc
    answer = Answer(
        answer=result.answer,
        lookups=[Lookup(tool=c.name, arguments=c.args, error=r.get("error")) for c, r in result.steps],
        cited=result.cited, unsupported_citations=result.unsupported,
        citations_verified=not any(result.unsupported.values()),
        stopped_early=result.stopped_early, model=getattr(model, "model", None),
        attribution_warnings=result.attribution_warnings)
    if (answer.citations_verified and not answer.stopped_early and not answer.attribution_warnings
            and not follow_up):
        # Only complete answers whose citations checked out are worth repeating.
        conn.execute(
            "INSERT INTO api_answer_cache (question_key, question, response) VALUES (%s, %s, %s) "
            "ON CONFLICT (question_key) DO UPDATE SET question = EXCLUDED.question, "
            "response = EXCLUDED.response, created_at = now()",
            (key, body.question, Jsonb(answer.model_dump())))
        conn.commit()
    return answer
