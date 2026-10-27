"""Human-readable review of ingested trials, as Markdown.

Usage:
    python -m neuroscope.review --limit 10                       # any origin
    python -m neuroscope.review --limit 10 --origin live_api --out review.md

For each trial: source link, data origin, phase, status, source and retrieval
dates, and every intervention's ORIGINAL registered name next to the drug it
resolved to and how that link was made.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect

ORIGIN_LABEL = {
    "live_api": "LIVE (ClinicalTrials.gov API)",
    "replay": "REPLAY (saved live API data)",
    "synthetic": "SYNTHETIC TEST FIXTURE (not a real trial)",
}


def _md(v) -> str:
    return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")


def build_review(conn, limit: int = 10, origin: str | None = None,
                 nct_ids: list[str] | None = None) -> str:
    where, params = ["TRUE"], []
    if origin:
        where.append("t.data_origin = %s")
        params.append(origin)
    if nct_ids:
        where.append("t.nct_id = ANY(%s)")
        params.append(nct_ids)
    trials = conn.execute(
        f"""
        SELECT t.nct_id, t.brief_title, t.data_origin, t.phase_label, t.overall_status, t.status_group,
               t.study_type, s.name, t.last_update_posted, t.last_retrieved_at::timestamp(0)
        FROM trials t LEFT JOIN sponsors s ON s.id = t.lead_sponsor_id
        WHERE {' AND '.join(where)}
        ORDER BY EXISTS (SELECT 1 FROM trial_interventions ti
                         WHERE ti.nct_id = t.nct_id AND ti.drug_id IS NOT NULL) DESC,
                 t.highest_phase DESC NULLS LAST, t.nct_id
        LIMIT %s
        """,
        params + [limit],
    ).fetchall()

    origins = sorted({t[2] for t in trials})
    out = [f"# NeuroScope trial review ({len(trials)} trials)", ""]
    out.append("Data origin in this report: " + ", ".join(ORIGIN_LABEL[o] for o in origins) if origins
               else "No trials matched.")
    out.append("")
    for (nct, title, org, phase, status, group, stype, sponsor, updated, retrieved) in trials:
        link = (f"https://clinicaltrials.gov/study/{nct}" if org != "synthetic"
                else "(synthetic fixture, no real record)")
        out += [
            f"## {nct}: {_md(title)}",
            "",
            f"- Origin: {ORIGIN_LABEL[org]}",
            f"- Source: {link}",
            f"- Type: {stype}; Phase: {phase}; Status: {status} ({group})",
            f"- Lead sponsor: {_md(sponsor)}",
            f"- Source last updated: {updated}; Retrieved: {retrieved} UTC",
            "",
            "| Original registered name | Other names (as registered) | Type | Resolved drug | How linked | Alias status |",
            "|---|---|---|---|---|---|",
        ]
        ivs = conn.execute(
            """
            SELECT ti.name, ti.other_names, ti.intervention_type, d.canonical_name,
                   ti.resolution_method, ti.matched_alias_key, a.review_status
            FROM trial_interventions ti
            LEFT JOIN drugs d ON d.id = ti.drug_id
            LEFT JOIN drug_aliases a ON a.alias_key = ti.matched_alias_key
            WHERE ti.nct_id = %s ORDER BY ti.id
            """,
            (nct,),
        ).fetchall()
        for name, others, itype, drug, method, mkey, astatus in ivs:
            how = f"{method} on '{mkey}'" if mkey else method
            out.append(f"| {_md(name)} | {_md('; '.join(others or []))} | {_md(itype)} | "
                       f"{_md(drug) or '(none)'} | {_md(how)} | {_md(astatus) or 'n/a'} |")
        if not ivs:
            out.append("| (no interventions registered) | | | | | |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--origin", choices=["live_api", "replay", "synthetic"])
    p.add_argument("--out", help="write Markdown here instead of stdout")
    args = p.parse_args(argv)
    with connect() as conn:
        text = build_review(conn, args.limit, args.origin)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
