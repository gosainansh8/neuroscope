"""Embeddings for document chunks (Google Vertex AI), enabling semantic search.

Usage:
    python -m neuroscope.embed status                 # how many chunks have embeddings
    python -m neuroscope.embed run                    # embed every chunk that needs it
    python -m neuroscope.embed run --limit 50         # a small first batch

Setup (once): `pip install -e ".[vertex]"`, `gcloud auth application-default login`,
and set GOOGLE_CLOUD_PROJECT (and optionally GOOGLE_CLOUD_LOCATION) in .env.

An embedding is a list of 768 numbers that places a piece of text in "meaning
space": texts about the same thing land close together even when they share
no words. Chunks are embedded once and stored in chunks.embedding; a search
embeds the question and finds the nearest chunks.

A chunk is (re)embedded when it has no embedding or was embedded by a
different model, so changing EMBEDDING_MODEL re-embeds everything on the next
run. Changed abstracts get new chunks, which start without embeddings.

Documents and questions are embedded with different task types
(RETRIEVAL_DOCUMENT and RETRIEVAL_QUERY), as the model expects.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, Protocol, Sequence, TypeVar

import psycopg

from .db import connect

EMBEDDING_MODEL = "text-embedding-005"
EMBEDDING_DIM = 768                       # must match chunks.embedding vector(768) in db/schema.sql
BATCH_SIZE = 40                           # about 12k tokens per request at 220-word chunks

# Statuses worth retrying: rate limited, or the service failed or timed out. A 400, 403, or 404
# means the request itself is wrong (bad model name, API disabled), so it is not retried.
RETRY_STATUS = {429, 500, 502, 503, 504}
T = TypeVar("T")


def call_with_retry(fn: Callable[[], T], attempts: int = 3, base_delay_s: float = 2.0,
                    sleep: Callable[[float], None] = time.sleep) -> T:
    """Call fn, retrying transient failures with a growing wait (2s, then 4s).

    An exception with an HTTP status is retried only for RETRY_STATUS. One without a status
    (a timeout or dropped connection from the HTTP layer) is retried too.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            code = getattr(exc, "code", None)
            transient = code in RETRY_STATUS if isinstance(code, int) else True
            if not transient or attempt == attempts:
                raise
            sleep(base_delay_s * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


class Embedder(Protocol):
    """Anything that turns texts into vectors. Tests use a fake; production uses VertexEmbedder."""
    model: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class VertexEmbedder:
    def __init__(self, project: str | None = None, location: str | None = None,
                 model: str = EMBEDDING_MODEL):
        project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set. See the setup steps in the README.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError('The Vertex AI SDK is not installed. Run: pip install -e ".[vertex]"') from exc
        self.model = model
        self._types = types
        self._client = genai.Client(vertexai=True, project=project,
                                    location=location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
                                    http_options=types.HttpOptions(timeout=60_000))   # fail, don't hang

    def _embed(self, texts: Sequence[str], task_type: str) -> list[list[float]]:
        from google.genai import errors
        try:
            resp = call_with_retry(lambda: self._client.models.embed_content(
                model=self.model, contents=list(texts),
                config=self._types.EmbedContentConfig(task_type=task_type, output_dimensionality=EMBEDDING_DIM)))
        except errors.APIError as exc:
            # One readable line: the status and Google's own first sentence, which names the fix.
            message = (getattr(exc, "message", None) or str(exc)).split(". If you ")[0]
            raise RuntimeError(f"Vertex AI request failed ({getattr(exc, 'code', '?')} "
                               f"{getattr(exc, 'status', '')}): {message}") from exc
        except Exception as exc:                 # timeouts and connection errors from the HTTP layer
            raise RuntimeError(f"Vertex AI request did not complete: {type(exc).__name__}: {exc}") from exc
        return [list(e.values) for e in resp.embeddings]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


def to_pgvector(vector: Sequence[float]) -> str:
    """pgvector's text form: '[0.1,0.2,...]'. Rejects a vector of the wrong size."""
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(f"expected {EMBEDDING_DIM} dimensions, got {len(vector)}")
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def status(conn: psycopg.Connection, model: str = EMBEDDING_MODEL) -> dict[str, int]:
    total, current, other = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL AND embedding_model = %s), "
        "COUNT(*) FILTER (WHERE embedding IS NOT NULL AND embedding_model IS DISTINCT FROM %s) FROM chunks",
        (model, model)).fetchone()
    return {"chunks": total, "embedded": current, "other_model": other, "to_embed": total - current}


def embed_chunks(conn: psycopg.Connection, embedder: Embedder, limit: int | None = None,
                 batch_size: int = BATCH_SIZE) -> dict[str, int]:
    """Embed chunks that have no embedding from this model. Commits after every batch,
    so an interrupted run keeps what it finished and the next run continues."""
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s "
        "ORDER BY id LIMIT %s", (embedder.model, limit)).fetchall()
    conn.commit()
    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        vectors = embedder.embed_documents([text for _, text in batch])
        if len(vectors) != len(batch):
            raise RuntimeError(f"asked for {len(batch)} embeddings, got {len(vectors)}")
        with conn.transaction():
            for (chunk_id, _), vector in zip(batch, vectors):
                conn.execute("UPDATE chunks SET embedding = %s::vector, embedding_model = %s, "
                             "embedded_at = now() WHERE id = %s", (to_pgvector(vector), embedder.model, chunk_id))
        done += len(batch)
    return {"embedded": done, "remaining": status(conn, embedder.model)["to_embed"]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sp = sub.add_parser("run")
    sp.add_argument("--limit", type=int, default=None, help="embed at most this many chunks")
    args = p.parse_args(argv)
    with connect() as conn:
        if args.cmd == "status":
            s = status(conn)
            print(f"{s['embedded']} of {s['chunks']} chunks embedded with {EMBEDDING_MODEL}; "
                  f"{s['to_embed']} to embed ({s['other_model']} have another model's embedding)")
            return 0
        try:
            embedder = VertexEmbedder()
            r = embed_chunks(conn, embedder, args.limit)
        except RuntimeError as exc:
            print(exc)
            return 2
    print(f"Embedded {r['embedded']} chunks with {EMBEDDING_MODEL}; {r['remaining']} remaining")
    return 0


if __name__ == "__main__":
    sys.exit(main())
