"""Ingest ClinicalTrials.gov studies into Postgres, with change tracking.

Usage:
    python -m neuroscope.ingest                                # Alzheimer's, live API
    python -m neuroscope.ingest --disease parkinsons           # another disease from the diseases table
    python -m neuroscope.ingest --limit 50                     # quick smoke test
    python -m neuroscope.ingest --save-raw data/raw/ad.jsonl   # also keep the raw JSON
    python -m neuroscope.ingest --from-jsonl data/raw/ad.jsonl # replay offline

Each study is processed in its own transaction. One malformed record is
counted as failed and skipped; it never aborts the run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from typing import Iterable

import psycopg
from psycopg.types.json import Jsonb

from .ctgov import TrialRecord, content_hash, iter_studies, iter_studies_from_jsonl, parse_study
from .db import connect
from .resolver import resolve_intervention

log = logging.getLogger("neuroscope.ingest")

DEFAULT_CONDITION = "Alzheimer Disease"

# Fields whose changes are worth surfacing in "what changed since last week?"
TRACKED_FIELDS = [
    "overall_status",
    "phase_label",
    "enrollment",
    "enrollment_type",
    "primary_completion_date",
    "completion_date",
    "has_results",
    "why_stopped",
]


@dataclass
class RunSummary:
    run_id: int
    fetched: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    drugs_created: int = 0
    conflicts: int = 0
    off_topic: int = 0          # returned by the disease query but not about the disease: skipped


def _upsert_sponsor(cur, sponsor: tuple[str, str | None] | None) -> int | None:
    if not sponsor:
        return None
    name, klass = sponsor
    cur.execute(
        "INSERT INTO sponsors (name, class) VALUES (%s, %s) "
        "ON CONFLICT (name) DO UPDATE SET class = COALESCE(EXCLUDED.class, sponsors.class) "
        "RETURNING id",
        (name, klass),
    )
    return cur.fetchone()[0]


def _str(v) -> str | None:
    return None if v is None else str(v)


def process_study(cur, raw: dict, run_id: int, summary: RunSummary, origin: str) -> str:
    """Write one study. Returns 'new', 'changed', or 'unchanged'."""
    rec: TrialRecord = parse_study(raw)
    h = content_hash(raw)

    cur.execute(
        "SELECT content_hash, " + ", ".join(TRACKED_FIELDS) + " FROM trials WHERE nct_id = %s",
        (rec.nct_id,),
    )
    existing = cur.fetchone()

    if existing and existing[0] == h:
        cur.execute("UPDATE trials SET last_retrieved_at = now() WHERE nct_id = %s", (rec.nct_id,))
        return "unchanged"

    lead_id = _upsert_sponsor(cur, rec.lead_sponsor)
    cur.execute(
        """
        INSERT INTO trials (
            nct_id, brief_title, official_title, study_type, overall_status, status_group,
            why_stopped, phases, phase_label, highest_phase, enrollment, enrollment_type,
            allocation, masking, start_date, start_date_precision, primary_completion_date,
            primary_completion_precision, completion_date, completion_precision,
            last_update_posted, has_results, lead_sponsor_id, conditions, keywords,
            content_hash, data_origin, last_retrieved_at
        ) VALUES (
            %(nct_id)s, %(brief_title)s, %(official_title)s, %(study_type)s, %(overall_status)s,
            %(status_group)s, %(why_stopped)s, %(phases)s, %(phase_label)s, %(highest_phase)s,
            %(enrollment)s, %(enrollment_type)s, %(allocation)s, %(masking)s, %(start_date)s,
            %(start_date_precision)s, %(primary_completion_date)s, %(primary_completion_precision)s,
            %(completion_date)s, %(completion_precision)s, %(last_update_posted)s, %(has_results)s,
            %(lead_sponsor_id)s, %(conditions)s, %(keywords)s, %(content_hash)s, %(data_origin)s, now()
        )
        ON CONFLICT (nct_id) DO UPDATE SET
            brief_title = EXCLUDED.brief_title, official_title = EXCLUDED.official_title,
            study_type = EXCLUDED.study_type, overall_status = EXCLUDED.overall_status,
            status_group = EXCLUDED.status_group, why_stopped = EXCLUDED.why_stopped,
            phases = EXCLUDED.phases, phase_label = EXCLUDED.phase_label,
            highest_phase = EXCLUDED.highest_phase, enrollment = EXCLUDED.enrollment,
            enrollment_type = EXCLUDED.enrollment_type, allocation = EXCLUDED.allocation,
            masking = EXCLUDED.masking, start_date = EXCLUDED.start_date,
            start_date_precision = EXCLUDED.start_date_precision,
            primary_completion_date = EXCLUDED.primary_completion_date,
            primary_completion_precision = EXCLUDED.primary_completion_precision,
            completion_date = EXCLUDED.completion_date,
            completion_precision = EXCLUDED.completion_precision,
            last_update_posted = EXCLUDED.last_update_posted, has_results = EXCLUDED.has_results,
            lead_sponsor_id = EXCLUDED.lead_sponsor_id, conditions = EXCLUDED.conditions,
            keywords = EXCLUDED.keywords, content_hash = EXCLUDED.content_hash,
            data_origin = EXCLUDED.data_origin, last_retrieved_at = now()
        """,
        {**rec.__dict__, "lead_sponsor_id": lead_id, "content_hash": h, "data_origin": origin},
    )

    # Child rows are replaced wholesale; simpler and safer than diffing them.
    cur.execute("DELETE FROM trial_collaborators WHERE nct_id = %s", (rec.nct_id,))
    for collab in rec.collaborators:
        sid = _upsert_sponsor(cur, collab)
        cur.execute(
            "INSERT INTO trial_collaborators (nct_id, sponsor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (rec.nct_id, sid),
        )

    cur.execute("DELETE FROM trial_interventions WHERE nct_id = %s", (rec.nct_id,))
    for iv in rec.interventions:
        res = resolve_intervention(
            cur, nct_id=rec.nct_id, name=iv.name,
            intervention_type=iv.intervention_type, other_names=iv.other_names,
        )
        summary.drugs_created += int(res.created)
        summary.conflicts += len(res.conflicts)
        cur.execute(
            "INSERT INTO trial_interventions (nct_id, name, intervention_type, description, "
            "other_names, arm_group_labels, is_placebo, drug_id, resolution_method, matched_alias_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (rec.nct_id, iv.name, iv.intervention_type, iv.description, iv.other_names,
             iv.arm_group_labels, res.method == "placebo", res.drug_id, res.method, res.matched_key),
        )

    cur.execute("DELETE FROM trial_outcomes WHERE nct_id = %s", (rec.nct_id,))
    for o in rec.outcomes:
        cur.execute(
            "INSERT INTO trial_outcomes (nct_id, kind, measure, description, time_frame) "
            "VALUES (%s, %s, %s, %s, %s)",
            (rec.nct_id, o.kind, o.measure, o.description, o.time_frame),
        )

    cur.execute(
        "INSERT INTO trial_snapshots (nct_id, content_hash, run_id, last_update_posted, raw) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (rec.nct_id, h, run_id, rec.last_update_posted, Jsonb(raw)),
    )

    if not existing:
        cur.execute(
            "INSERT INTO trial_changes (run_id, nct_id, field, old_value, new_value) "
            "VALUES (%s, %s, 'new_trial', NULL, %s)",
            (run_id, rec.nct_id, rec.phase_label),
        )
        return "new"

    old = dict(zip(TRACKED_FIELDS, existing[1:]))
    for f in TRACKED_FIELDS:
        before, after = _str(old[f]), _str(getattr(rec, f))
        if before != after:
            cur.execute(
                "INSERT INTO trial_changes (run_id, nct_id, field, old_value, new_value) "
                "VALUES (%s, %s, %s, %s, %s)",
                (run_id, rec.nct_id, f, before, after),
            )
    return "changed"


def run_ingestion(conn: psycopg.Connection, studies: Iterable[dict], query: str,
                  origin: str, save_raw_path: str | None = None, disease: str | None = None) -> RunSummary:
    """origin is 'live_api', 'replay', or 'synthetic' and is stamped on every trial.
    `disease` (a diseases.slug) tags every on-topic study this query returned as belonging to that
    disease. A study whose conditions, keywords, and titles do not match the disease's topic
    pattern is skipped and counted as off_topic (registry synonyms return such studies)."""
    if origin not in ("live_api", "replay", "synthetic"):
        raise ValueError(f"bad origin: {origin}")
    cur = conn.execute("INSERT INTO ingestion_runs (query, data_origin) VALUES (%s, %s) RETURNING id",
                       (query, origin))
    run_id = cur.fetchone()[0]
    conn.commit()
    summary = RunSummary(run_id=run_id)
    topic = topic_regex(conn, disease) if disease else None

    raw_fh = open(save_raw_path, "w", encoding="utf-8") if save_raw_path else None
    try:
        for raw in studies:
            summary.fetched += 1
            if raw_fh:
                raw_fh.write(json.dumps(raw) + "\n")
            nct = (raw.get("protocolSection") or {}).get("identificationModule", {}).get("nctId", "?")
            if topic and not on_topic(raw, topic):
                summary.off_topic += 1
                continue
            try:
                with conn.transaction():
                    with conn.cursor() as c:
                        outcome = process_study(c, raw, run_id, summary, origin)
                        if disease:
                            c.execute("INSERT INTO trial_diseases (nct_id, disease, first_run_id) "
                                      "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (nct, disease, run_id))
                setattr(summary, outcome, getattr(summary, outcome) + 1)
            except Exception as exc:  # one bad record must not stop the run
                summary.failed += 1
                log.warning("failed to ingest %s: %s", nct, exc)
            if summary.fetched % 250 == 0:
                log.info("processed %d studies", summary.fetched)

        conn.execute(
            "UPDATE ingestion_runs SET finished_at = now(), status = 'succeeded', n_fetched = %s, "
            "n_new = %s, n_changed = %s, n_unchanged = %s, n_failed = %s WHERE id = %s",
            (summary.fetched, summary.new, summary.changed, summary.unchanged, summary.failed, run_id),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE ingestion_runs SET finished_at = now(), status = 'failed', error = %s, "
            "n_fetched = %s WHERE id = %s",
            (str(exc)[:2000], summary.fetched, run_id),
        )
        conn.commit()
        raise
    finally:
        if raw_fh:
            raw_fh.close()
    return summary


def topic_regex(conn: psycopg.Connection, disease: str) -> re.Pattern:
    pattern = conn.execute("SELECT topic_pattern FROM diseases WHERE slug = %s", (disease,)).fetchone()[0]
    return re.compile(pattern, re.IGNORECASE)


def topic_text(conditions, keywords, brief_title, official_title) -> str:
    return " ".join([*(conditions or []), *(keywords or []), brief_title or "", official_title or ""])


def on_topic(raw: dict, topic: re.Pattern) -> bool:
    ps = raw.get("protocolSection") or {}
    cond = ps.get("conditionsModule") or {}
    ident = ps.get("identificationModule") or {}
    return bool(topic.search(topic_text(cond.get("conditions"), cond.get("keywords"),
                                        ident.get("briefTitle"), ident.get("officialTitle"))))


def prune_off_topic(conn: psycopg.Connection) -> dict[str, int]:
    """Drop disease tags whose trial is not about that disease, then delete trials left with no
    disease (with their snapshots, change rows, and conflicts), then re-link interventions so drugs
    used only by deleted trials are removed. Does nothing before any trial has a disease tag."""
    from .curate import reresolve
    if not conn.execute("SELECT 1 FROM trial_diseases LIMIT 1").fetchone():
        return {"tags_removed": 0, "trials_deleted": 0}
    patterns = {slug: re.compile(p, re.IGNORECASE) for slug, p in
                conn.execute("SELECT slug, topic_pattern FROM diseases").fetchall()}
    rows = conn.execute(
        "SELECT td.nct_id, td.disease, t.conditions, t.keywords, t.brief_title, t.official_title "
        "FROM trial_diseases td JOIN trials t ON t.nct_id = td.nct_id").fetchall()
    stale = [(n, d) for n, d, *text in rows if not patterns[d].search(topic_text(*text))]
    with conn.transaction():
        for nct, disease in stale:
            conn.execute("DELETE FROM trial_diseases WHERE nct_id = %s AND disease = %s", (nct, disease))
        orphans = [r[0] for r in conn.execute(
            "SELECT nct_id FROM trials t WHERE NOT EXISTS (SELECT 1 FROM trial_diseases td "
            "WHERE td.nct_id = t.nct_id)")]
        for table in ("trial_snapshots", "trial_changes", "drug_alias_conflicts"):
            conn.execute(f"DELETE FROM {table} WHERE nct_id = ANY(%s)", (orphans,))
        conn.execute("DELETE FROM trials WHERE nct_id = ANY(%s)", (orphans,))
    if orphans:
        reresolve(conn)
    return {"tags_removed": len(stale), "trials_deleted": len(orphans)}


def condition_for(conn: psycopg.Connection, disease: str) -> str:
    row = conn.execute("SELECT ct_condition FROM diseases WHERE slug = %s", (disease,)).fetchone()
    if not row:
        known = [r[0] for r in conn.execute("SELECT slug FROM diseases ORDER BY sort_order")]
        raise SystemExit(f"Unknown disease {disease!r}. Known: {', '.join(known)}")
    return row[0]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--disease", default="alzheimers", help="a diseases.slug: alzheimers, parkinsons, als")
    p.add_argument("--condition", default=None, help="a raw ClinicalTrials.gov condition query, untagged")
    p.add_argument("--limit", type=int, default=None, help="stop after N studies")
    p.add_argument("--page-size", type=int, default=200, help="studies per API page (max 1000)")
    p.add_argument("--save-raw", metavar="PATH", help="write raw study JSON lines here")
    p.add_argument("--from-jsonl", metavar="PATH", help="replay studies from a saved file")
    p.add_argument("--prune-off-topic", action="store_true",
                   help="remove trials whose text does not name any disease they were tagged with, then exit")
    p.add_argument("--synthetic", action="store_true",
                   help="with --from-jsonl: label the data as synthetic test fixtures")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.from_jsonl:
        studies = iter_studies_from_jsonl(args.from_jsonl)
        query = f"file:{args.from_jsonl}"
        origin = "synthetic" if args.synthetic else "replay"
    disease = None if args.condition else args.disease
    if args.prune_off_topic:
        with connect() as conn:
            r = prune_off_topic(conn)
        print(f"Removed {r['tags_removed']} off-topic disease tags and deleted {r['trials_deleted']} trials")
        return 0
    with connect() as conn:
        if not args.from_jsonl:
            condition = args.condition or condition_for(conn, disease)
            studies = iter_studies(condition, page_size=args.page_size, max_studies=args.limit)
            query = f"cond:{condition}"
            origin = "live_api"
        s = run_ingestion(conn, studies, query, origin, save_raw_path=args.save_raw, disease=disease)

    print(
        f"Run {s.run_id}: fetched {s.fetched}, new {s.new}, changed {s.changed}, "
        f"unchanged {s.unchanged}, failed {s.failed}, off-topic skipped {s.off_topic}; "
        f"drugs created {s.drugs_created}, alias conflicts {s.conflicts}"
    )
    return 1 if s.failed and s.failed == s.fetched else 0


if __name__ == "__main__":
    sys.exit(main())
