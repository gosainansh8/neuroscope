"""Bring every source up to date in one run. Meant to run weekly (see deploy/4_schedule_refresh.sh).

Usage:
    python -m neuroscope.refresh                      # all steps
    python -m neuroscope.refresh --steps trials,fda   # some steps

Steps, in order:
    schema       apply migrations (db init), so the database matches the code
    trials       re-fetch every Alzheimer's study from ClinicalTrials.gov; changes go to trial_changes
    fda          download the latest Drugs@FDA export and rebuild the FDA approval rows
    ema          download the EMA medicines list and rebuild the EU authorisation rows
    labels       re-fetch FDA label indications for the approved applications
    literature   fetch PubMed abstracts for the most-studied drugs (new ones are added, changed ones re-chunked)
    embeddings   embed passages that have no embedding yet (Vertex AI)
    cache        clear saved answers, so no answer predates the new data

Each step runs on its own: a failure is recorded and the next step still runs.
The run is recorded in refresh_runs. Exit code: 0 if every step succeeded,
1 if some failed, 2 if all failed.

New trials are resolved with the usual rules, so new names wait in the review
queue (`curate unreviewed`) as before. Review them where the refresh ran.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from .db import ROOT, connect, init_db

log = logging.getLogger("neuroscope.refresh")

REFRESH_TABLE_SQL = ROOT / "db" / "migrations" / "005_refresh_runs.sql"
LITERATURE_MIN_TRIALS, LITERATURE_PER_DRUG = 2, 40     # drugs with 2+ trials, 40 abstracts each
STEPS = ["schema", "trials", "fda", "ema", "labels", "literature", "embeddings", "cache"]


def step_schema(conn: psycopg.Connection) -> dict:
    return init_db(conn)


def step_trials(conn: psycopg.Connection) -> dict:
    from .ctgov import iter_studies
    from .ingest import DEFAULT_CONDITION, run_ingestion
    s = run_ingestion(conn, iter_studies(DEFAULT_CONDITION, page_size=200),
                      f"cond:{DEFAULT_CONDITION} (weekly refresh)", "live_api")
    changes = dict(conn.execute("SELECT field, COUNT(*) FROM trial_changes WHERE run_id = %s GROUP BY 1",
                                (s.run_id,)).fetchall())
    return {"run_id": s.run_id, "fetched": s.fetched, "new": s.new, "changed": s.changed,
            "unchanged": s.unchanged, "failed": s.failed, "changes_by_field": changes}


def step_fda(conn: psycopg.Connection) -> dict:
    from pathlib import Path

    from .fda import download, load
    with tempfile.TemporaryDirectory() as tmp:
        return load(conn, download(Path(tmp)))


def step_ema(conn: psycopg.Connection) -> dict:
    from pathlib import Path

    from .ema import download, load
    with tempfile.TemporaryDirectory() as tmp:
        return load(conn, download(Path(tmp)))


def step_labels(conn: psycopg.Connection) -> dict:
    from .fda import iter_labels, labelled_application_numbers, load_labels
    return load_labels(conn, iter(list(iter_labels(labelled_application_numbers(conn)))))


def step_literature(conn: psycopg.Connection) -> dict:
    from .pubmed import iter_live, store
    skipped: list[str] = []
    abstracts = list(iter_live(conn, min_trials=LITERATURE_MIN_TRIALS, per_drug=LITERATURE_PER_DRUG, max_drugs=None, skipped=skipped))
    if not abstracts and skipped:
        raise RuntimeError(f"PubMed failed for every drug tried ({len(skipped)})")
    return {**store(conn, iter(abstracts)), "drugs_skipped": skipped}


def step_embeddings(conn: psycopg.Connection) -> dict:
    from .embed import VertexEmbedder, embed_chunks
    return embed_chunks(conn, VertexEmbedder())


def step_cache(conn: psycopg.Connection) -> dict:
    n = conn.execute("DELETE FROM api_answer_cache").rowcount
    conn.commit()
    return {"saved_answers_cleared": n}


STEP_FUNCTIONS: dict[str, Callable[[psycopg.Connection], dict]] = {
    "schema": step_schema, "trials": step_trials, "fda": step_fda, "ema": step_ema, "labels": step_labels,
    "literature": step_literature, "embeddings": step_embeddings, "cache": step_cache,
}


def run_refresh(conn: psycopg.Connection, steps: list[str], functions=None) -> dict:
    """Run the steps in order, each on its own; record the run in refresh_runs."""
    functions = functions or STEP_FUNCTIONS
    # The table that records runs may not exist yet on an older database.
    conn.execute(REFRESH_TABLE_SQL.read_text(encoding="utf-8"))
    conn.commit()
    run_id = conn.execute("INSERT INTO refresh_runs DEFAULT VALUES RETURNING id").fetchone()[0]
    conn.commit()
    results = []
    for name in steps:
        started = time.monotonic()
        try:
            summary = functions[name](conn)
            results.append({"step": name, "ok": True, "seconds": round(time.monotonic() - started, 1),
                            "summary": summary})
            log.info("step %s done: %s", name, summary)
        except Exception as exc:  # one failed source must not stop the others
            conn.rollback()
            results.append({"step": name, "ok": False, "seconds": round(time.monotonic() - started, 1),
                            "error": f"{type(exc).__name__}: {exc}"[:1000]})
            log.error("step %s failed: %s", name, exc)
        conn.execute("UPDATE refresh_runs SET steps = %s WHERE id = %s", (Jsonb(results), run_id))
        conn.commit()
    ok = sum(r["ok"] for r in results)
    status = "succeeded" if ok == len(results) else ("failed" if ok == 0 else "partial")
    conn.execute("UPDATE refresh_runs SET finished_at = now(), status = %s WHERE id = %s", (status, run_id))
    conn.commit()
    return {"run_id": run_id, "status": status, "steps": results}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--steps", default=",".join(STEPS), help="comma-separated, from: " + ", ".join(STEPS))
    args = p.parse_args(argv)
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEP_FUNCTIONS]
    if unknown:
        print(f"Unknown step(s): {', '.join(unknown)}. Choose from: {', '.join(STEPS)}")
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with connect() as conn:
        r = run_refresh(conn, steps)
    print(f"Refresh run {r['run_id']}: {r['status']}")
    for s in r["steps"]:
        print(f"  {'OK  ' if s['ok'] else 'FAIL'} {s['step']:<11} {s['seconds']:>6}s  "
              f"{s.get('summary') if s['ok'] else s['error']}")
    return 0 if r["status"] == "succeeded" else (2 if r["status"] == "failed" else 1)


if __name__ == "__main__":
    sys.exit(main())
