"""Human review and correction of drug aliases.

Usage:
    python -m neuroscope.curate unreviewed                 # learned aliases, most trials waiting first
    python -m neuroscope.curate conflicts                  # names pointing at 2+ drugs
    python -m neuroscope.curate ambiguous                  # names listing alternatives ("A or B")
    python -m neuroscope.curate verify  "LY3372993"        # confirm an alias
    python -m neuroscope.curate reject  "AB-123"           # never match this name again
    python -m neuroscope.curate map     "DEF-200" defumab  # point a name at a drug (created if new)
    python -m neuroscope.curate resolve-conflict 12 --note "checked sponsor pipeline"
    python -m neuroscope.curate reresolve                  # re-link every intervention

HOW AN INCORRECT MERGE IS REVERSED
Trial links are never the source of truth; aliases are. Every intervention
row keeps its original registered name and otherNames exactly as CT.gov
published them. So a correction is two steps:
  1. Fix the alias table (map the wrong name to the right drug, or reject it).
  2. Run `reresolve`, which recomputes every intervention's drug from its
     original names against the corrected aliases.
No trial, intervention, or original name is deleted. Auto-created drugs left
with no interventions and no verified aliases are removed. Every correction is
written to alias_corrections.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from .db import connect
from .normalize import alias_key, combination_display, combination_key, split_combination
from .resolver import resolve_intervention


def _key_for(name: str) -> tuple[str, bool]:
    comps = split_combination(name)
    return (combination_key(comps), True) if comps else (alias_key(name), False)


def _audit(conn, action, key=None, old=None, new=None, note=None):
    conn.execute(
        "INSERT INTO alias_corrections (action, alias_key, old_drug_id, new_drug_id, note) "
        "VALUES (%s, %s, %s, %s, %s)",
        (action, key, old, new, note),
    )


def verify_alias(conn: psycopg.Connection, name: str, note: str | None = None) -> None:
    key, _ = _key_for(name)
    with conn.transaction():
        row = conn.execute("SELECT drug_id, review_status FROM drug_aliases WHERE alias_key = %s",
                           (key,)).fetchone()
        if not row:
            raise ValueError(f"no alias with key {key!r}")
        if row[1] == "rejected":
            raise ValueError(f"{key!r} is rejected; use `map` to point it at a drug")
        conn.execute("UPDATE drug_aliases SET review_status = 'verified', reviewed_at = now() "
                     "WHERE alias_key = %s", (key,))
        _audit(conn, "verify", key, row[0], row[0], note)


def reject_alias(conn: psycopg.Connection, name: str, note: str | None = None) -> None:
    """Tombstone: the key stays in the table so ingestion can never re-learn it."""
    key, is_combo = _key_for(name)
    with conn.transaction():
        row = conn.execute("SELECT drug_id FROM drug_aliases WHERE alias_key = %s", (key,)).fetchone()
        conn.execute(
            "INSERT INTO drug_aliases (alias_key, alias_display, drug_id, alias_type, source, "
            "review_status, reviewed_at) VALUES (%s, %s, NULL, %s, 'manual', 'rejected', now()) "
            "ON CONFLICT (alias_key) DO UPDATE SET drug_id = NULL, review_status = 'rejected', "
            "reviewed_at = now()",
            (key, name, "combination" if is_combo else "other"),
        )
        _audit(conn, "reject", key, row[0] if row else None, None, note)


def map_alias(conn: psycopg.Connection, name: str, drug_name: str, note: str | None = None) -> int:
    """Point `name` at `drug_name` as a verified manual alias. Creates the drug if needed."""
    key, is_combo = _key_for(name)
    target_comps = split_combination(drug_name)
    if bool(target_comps) != is_combo:
        raise ValueError("a combination name can only map to a combination drug, and vice versa")
    canonical = combination_display(target_comps) if target_comps else drug_name.strip()
    with conn.transaction():
        row = conn.execute("SELECT id FROM drugs WHERE canonical_name = %s", (canonical,)).fetchone()
        if row:
            drug_id = row[0]
        else:
            drug_id = conn.execute(
                "INSERT INTO drugs (canonical_name, is_combination) VALUES (%s, %s) RETURNING id",
                (canonical, is_combo),
            ).fetchone()[0]
            # The drug's own name is a verified alias too.
            own_key, _ = _key_for(canonical)
            conn.execute(
                "INSERT INTO drug_aliases (alias_key, alias_display, drug_id, alias_type, source, "
                "review_status, reviewed_at) VALUES (%s, %s, %s, %s, 'manual', 'verified', now()) "
                "ON CONFLICT (alias_key) DO NOTHING",
                (own_key, canonical, drug_id, "combination" if is_combo else "other"),
            )
        old = conn.execute("SELECT drug_id FROM drug_aliases WHERE alias_key = %s", (key,)).fetchone()
        conn.execute(
            "INSERT INTO drug_aliases (alias_key, alias_display, drug_id, alias_type, source, "
            "review_status, reviewed_at) VALUES (%s, %s, %s, %s, 'manual', 'verified', now()) "
            "ON CONFLICT (alias_key) DO UPDATE SET drug_id = EXCLUDED.drug_id, source = 'manual', "
            "review_status = 'verified', reviewed_at = now()",
            (key, name, drug_id, "combination" if is_combo else "other"),
        )
        _audit(conn, "map", key, old[0] if old else None, drug_id, note)
    return drug_id


def resolve_conflict(conn: psycopg.Connection, conflict_id: int, note: str | None = None) -> None:
    with conn.transaction():
        n = conn.execute("UPDATE drug_alias_conflicts SET resolved = TRUE WHERE id = %s",
                         (conflict_id,)).rowcount
        if not n:
            raise ValueError(f"no conflict with id {conflict_id}")
        _audit(conn, "resolve_conflict", None, None, None, f"conflict {conflict_id}: {note or ''}")


def reresolve(conn: psycopg.Connection) -> dict[str, int]:
    """Recompute every intervention's drug link from its original names."""
    with conn.transaction():
        conn.execute("DELETE FROM drug_alias_conflicts WHERE NOT resolved")
        rows = conn.execute(
            "SELECT id, nct_id, name, intervention_type, other_names, drug_id "
            "FROM trial_interventions ORDER BY nct_id, id"
        ).fetchall()
        relinked = n_conflicts = 0
        with conn.cursor() as cur:
            for iv_id, nct_id, name, iv_type, other_names, old_drug in rows:
                res = resolve_intervention(cur, nct_id=nct_id, name=name,
                                           intervention_type=iv_type, other_names=other_names)
                n_conflicts += int(res.method == "conflict")
                if res.drug_id != old_drug:
                    relinked += 1
                cur.execute(
                    "UPDATE trial_interventions SET drug_id = %s, resolution_method = %s, "
                    "matched_alias_key = %s WHERE id = %s",
                    (res.drug_id, res.method, res.matched_key, iv_id),
                )
        orphans = conn.execute(
            "DELETE FROM drugs d WHERE NOT d.is_seeded "
            "AND NOT EXISTS (SELECT 1 FROM trial_interventions ti WHERE ti.drug_id = d.id) "
            "AND NOT EXISTS (SELECT 1 FROM drug_aliases a WHERE a.drug_id = d.id AND a.review_status = 'verified')"
        ).rowcount
        _audit(conn, "reresolve", note=f"{len(rows)} interventions, {relinked} relinked, "
                                       f"{n_conflicts} conflicts, {orphans} orphan drugs removed")
    return {"interventions": len(rows), "relinked": relinked,
            "conflicts": n_conflicts, "orphan_drugs_deleted": orphans}


def _print(rows, header):
    print(" | ".join(header))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("unreviewed")
    sub.add_parser("conflicts")
    sub.add_parser("ambiguous")
    for name in ("verify", "reject"):
        sp = sub.add_parser(name)
        sp.add_argument("alias")
        sp.add_argument("--note")
    sp = sub.add_parser("map")
    sp.add_argument("alias")
    sp.add_argument("drug")
    sp.add_argument("--note")
    sp = sub.add_parser("resolve-conflict")
    sp.add_argument("conflict_id", type=int)
    sp.add_argument("--note")
    sub.add_parser("reresolve")
    args = p.parse_args(argv)

    with connect() as conn:
        if args.cmd == "unreviewed":
            # trials_waiting: other trials left unlinked ('pending_review') until this alias is verified.
            _print(conn.execute(
                "SELECT a.alias_display, d.canonical_name, a.source, a.first_seen_nct, "
                "(SELECT COUNT(DISTINCT ti.nct_id) FROM trial_interventions ti "
                " WHERE ti.matched_alias_key = a.alias_key AND ti.resolution_method = 'pending_review') "
                " AS trials_waiting, "
                "(SELECT COUNT(DISTINCT ti.nct_id) FROM trial_interventions ti "
                " WHERE ti.matched_alias_key = a.alias_key AND ti.drug_id IS NOT NULL) AS trials_linked "
                "FROM drug_aliases a JOIN drugs d ON d.id = a.drug_id "
                "WHERE a.review_status = 'unreviewed' ORDER BY 5 DESC, 6 DESC, 2, 1").fetchall(),
                ["alias", "drug", "learned_from", "first_seen_nct", "trials_waiting", "trials_linked"])
        elif args.cmd == "conflicts":
            _print(conn.execute(
                "SELECT c.id, c.nct_id, c.intervention_name, c.alias_key, a.canonical_name, "
                "b.canonical_name FROM drug_alias_conflicts c "
                "LEFT JOIN drugs a ON a.id = c.assigned_drug_id "
                "LEFT JOIN drugs b ON b.id = c.conflicting_drug_id "
                "WHERE NOT c.resolved ORDER BY c.id").fetchall(),
                ["id", "nct_id", "intervention", "alias_key", "assigned (blank=unlinked)", "conflicting"])
        elif args.cmd == "ambiguous":
            # Left unlinked on purpose: the record does not say which drug the arm received.
            _print(conn.execute(
                "SELECT nct_id, name, array_to_string(other_names, '; ') FROM trial_interventions "
                "WHERE resolution_method = 'ambiguous_name' ORDER BY nct_id, id").fetchall(),
                ["nct_id", "registered name (as published)", "other names (as published)"])
        elif args.cmd == "verify":
            verify_alias(conn, args.alias, args.note)
            print("Verified. Run `reresolve` to link trials waiting on this alias.")
        elif args.cmd == "reject":
            reject_alias(conn, args.alias, args.note)
            print("Rejected. Run `reresolve` to re-link affected trials.")
        elif args.cmd == "map":
            map_alias(conn, args.alias, args.drug, args.note)
            print("Mapped. Run `reresolve` to re-link affected trials.")
        elif args.cmd == "resolve-conflict":
            resolve_conflict(conn, args.conflict_id, args.note)
        elif args.cmd == "reresolve":
            print(reresolve(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
