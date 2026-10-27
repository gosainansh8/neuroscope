"""Print a sanity-check summary of what is in the database.

Usage:
    python -m neuroscope.report

Run this after every ingestion and eyeball it. If a well-known drug is
missing, split in two, or merged with something else, fix the seed aliases
before building anything on top of the data.
"""

from __future__ import annotations

import sys

from .db import connect

QUERIES = {
    "Trials by status group (interventional only)": """
        SELECT status_group, COUNT(*) FROM trials
        WHERE study_type = 'INTERVENTIONAL' GROUP BY 1 ORDER BY 2 DESC
    """,
    "Trials by phase (interventional only)": """
        SELECT phase_label, COUNT(*) FROM trials
        WHERE study_type = 'INTERVENTIONAL' GROUP BY 1 ORDER BY 2 DESC
    """,
    "Top 20 drugs by number of trials": """
        SELECT canonical_name, n_trials, n_active_trials, highest_phase_any, highest_phase_active
        FROM v_drug_pipeline ORDER BY n_trials DESC, canonical_name LIMIT 20
    """,
    "Drugs with an active Phase 3 trial": """
        SELECT canonical_name, n_active_trials, lead_sponsors
        FROM v_drug_pipeline WHERE highest_phase_active >= 3
        ORDER BY canonical_name
    """,
    "Seeded drugs with zero trials (check aliases)": """
        SELECT d.canonical_name FROM drugs d
        WHERE d.is_seeded AND NOT EXISTS (SELECT 1 FROM trial_interventions ti WHERE ti.drug_id = d.id)
        ORDER BY 1
    """,
    "Unresolved alias conflicts (assigned blank = left unlinked)": """
        SELECT c.id, c.nct_id, c.intervention_name, a.canonical_name AS assigned, b.canonical_name AS conflicting
        FROM drug_alias_conflicts c
        LEFT JOIN drugs a ON a.id = c.assigned_drug_id
        LEFT JOIN drugs b ON b.id = c.conflicting_drug_id
        WHERE NOT c.resolved ORDER BY c.detected_at DESC LIMIT 25
    """,
    "Interventions naming alternatives, left unlinked (A or B)": """
        SELECT nct_id, name FROM trial_interventions
        WHERE resolution_method = 'ambiguous_name' ORDER BY nct_id LIMIT 25
    """,
    "Intervention links by resolution method": """
        SELECT resolution_method, COUNT(*) FROM trial_interventions GROUP BY 1 ORDER BY 2 DESC
    """,
    "Unreviewed aliases with the most trials waiting on review (review these first)": """
        SELECT a.alias_display, d.canonical_name, a.source, a.first_seen_nct, COUNT(DISTINCT ti.nct_id)
        FROM drug_aliases a
        JOIN drugs d ON d.id = a.drug_id
        JOIN trial_interventions ti ON ti.matched_alias_key = a.alias_key
        WHERE a.review_status = 'unreviewed' AND ti.resolution_method = 'pending_review'
        GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 15
    """,
    "Data origin of trials": """
        SELECT data_origin, COUNT(*) FROM trials GROUP BY 1
    """,
    "Most recent changes": """
        SELECT detected_at::date, nct_id, field, old_value, new_value
        FROM trial_changes WHERE field <> 'new_trial'
        ORDER BY detected_at DESC LIMIT 15
    """,
    "Recent ingestion runs": """
        SELECT id, query, status, n_fetched, n_new, n_changed, n_unchanged, n_failed, started_at::timestamp(0)
        FROM ingestion_runs ORDER BY id DESC LIMIT 5
    """,
}


def main() -> int:
    with connect() as conn:
        for title, sql in QUERIES.items():
            rows = conn.execute(sql).fetchall()
            print(f"\n== {title} ({len(rows)}) ==")
            for row in rows:
                print("  " + " | ".join("" if v is None else str(v) for v in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
