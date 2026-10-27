"""Live end-to-end check against ClinicalTrials.gov.

Usage:
    python -m neuroscope.live_check                    # 25 studies, 10 per page
    python -m neuroscope.live_check --limit 40 --page-size 15 --out live_review.md

What it proves:
  1. Pagination: a small page size forces several pages; the page count is checked.
  2. No duplicates: the same studies are fetched a second time by NCT ID, and
     row counts for those trials must not change.
  3. Missing fields: reports how many live records lacked each optional field,
     and that none of them failed to load.
  4. Writes a 10-trial review (source links, original names, resolved drugs).

Exit code is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys

from .ctgov import iter_studies
from .db import connect
from .ingest import DEFAULT_CONDITION, run_ingestion
from .review import build_review

COUNT_SQL = {
    "trials": "SELECT COUNT(*) FROM trials WHERE nct_id = ANY(%s)",
    "interventions": "SELECT COUNT(*) FROM trial_interventions WHERE nct_id = ANY(%s)",
    "outcomes": "SELECT COUNT(*) FROM trial_outcomes WHERE nct_id = ANY(%s)",
    "collaborators": "SELECT COUNT(*) FROM trial_collaborators WHERE nct_id = ANY(%s)",
    "snapshots": "SELECT COUNT(*) FROM trial_snapshots WHERE nct_id = ANY(%s)",
    "all_drugs": "SELECT COUNT(*) FROM drugs",
    "all_aliases": "SELECT COUNT(*) FROM drug_aliases",
}

MISSING_SQL = """
SELECT
  COUNT(*) FILTER (WHERE start_date IS NULL)              AS no_start_date,
  COUNT(*) FILTER (WHERE primary_completion_date IS NULL) AS no_primary_completion,
  COUNT(*) FILTER (WHERE enrollment IS NULL)              AS no_enrollment,
  COUNT(*) FILTER (WHERE highest_phase IS NULL)           AS no_phase,
  COUNT(*) FILTER (WHERE lead_sponsor_id IS NULL)         AS no_lead_sponsor,
  COUNT(*) FILTER (WHERE last_update_posted IS NULL)      AS no_last_update,
  COUNT(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM trial_interventions ti
                                     WHERE ti.nct_id = trials.nct_id)) AS no_interventions
FROM trials WHERE nct_id = ANY(%s)
"""


def counts(conn, ids):
    return {k: conn.execute(sql, (ids,) if "%s" in sql else None).fetchone()[0]
            for k, sql in COUNT_SQL.items()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--condition", default=DEFAULT_CONDITION)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--page-size", type=int, default=10)
    p.add_argument("--out", default="live_review.md")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    checks: list[tuple[str, bool, str]] = []
    with connect() as conn:
        # Run 1: query by condition, small pages.
        stats1: dict = {}
        fetched_ids: list[str] = []

        def tap(gen):
            for raw in gen:
                nct = (raw.get("protocolSection") or {}).get("identificationModule", {}).get("nctId")
                if nct:
                    fetched_ids.append(nct)
                yield raw

        s1 = run_ingestion(conn, tap(iter_studies(args.condition, page_size=args.page_size,
                                                  max_studies=args.limit, stats=stats1)),
                           f"cond:{args.condition} (live_check run 1)", "live_api")
        ids = sorted(set(fetched_ids))
        before = counts(conn, ids)
        print(f"Run 1: {s1}")
        print(f"  pages fetched: {stats1['pages']}, API total for condition: {stats1.get('total_count')}")

        want = min(args.limit, stats1.get("total_count", args.limit))
        expected_pages = math.ceil(want / args.page_size)
        checks.append(("pagination followed nextPageToken",
                       stats1["pages"] == expected_pages and len(fetched_ids) == want,
                       f"{stats1['pages']} pages (expected {expected_pages}), "
                       f"{len(fetched_ids)} studies (expected {want})"))
        checks.append(("no duplicate NCT IDs within a run", len(ids) == len(fetched_ids),
                       f"{len(fetched_ids)} fetched, {len(ids)} distinct"))
        checks.append(("no study failed to load", s1.failed == 0, f"{s1.failed} failed"))

        # Run 2: re-fetch exactly the same studies by ID.
        stats2: dict = {}
        s2 = run_ingestion(conn, iter_studies(nct_ids=ids, page_size=args.page_size, stats=stats2),
                           "live_check run 2 (same NCT IDs)", "live_api")
        after = counts(conn, ids)
        print(f"Run 2: {s2}")
        checks.append(("run 2 re-fetched every study", s2.fetched == len(ids),
                       f"{s2.fetched} of {len(ids)}"))
        checks.append(("run 2 created no new trials", s2.new == 0, f"new={s2.new}"))
        checks.append(("row counts unchanged after re-run", before == after,
                       "identical" if before == after else f"before={before} after={after}"))
        if s2.changed:
            print(f"  note: {s2.changed} trial(s) changed at the source between runs; "
                  "that is expected occasionally and is logged in trial_changes")

        missing = dict(zip(
            ["no_start_date", "no_primary_completion", "no_enrollment", "no_phase",
             "no_lead_sponsor", "no_last_update", "no_interventions"],
            conn.execute(MISSING_SQL, (ids,)).fetchone()))
        print(f"Missing optional fields across {len(ids)} live trials: {missing}")

        review = build_review(conn, limit=10, origin="live_api", nct_ids=ids)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(review + "\n")

    print("\nChecks:")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"\nReview of 10 live trials written to {args.out}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
