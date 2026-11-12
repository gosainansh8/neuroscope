"""Mechanisms of action and targets from ChEMBL (EMBL-EBI), for comparing pipelines.

Usage:
    python -m neuroscope.mechanisms load                 # drugs with 2+ trials (about 20 minutes)
    python -m neuroscope.mechanisms load --min-trials 1 --max-drugs 50

HOW A DRUG IS MATCHED TO A CHEMBL MOLECULE: ChEMBL is searched for the drug's name,
and a returned molecule is accepted only if its preferred name or one of its listed
synonyms has exactly the same key as a TRUSTED name of the drug (a verified alias or
the drug's own name). The same rule as FDA and EMA matching; a search hit alone is
never enough. Mechanisms are read from the molecule's parent (salts share it).

Drug classes and combinations are skipped. A drug with no mechanism in ChEMBL (most
early-stage codes) is simply absent. The load replaces the rows of every drug it
looks up, so re-running it picks up ChEMBL updates and alias corrections.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import psycopg
import requests

from .ctgov import _session
from .db import connect
from .fda import trusted_keys
from .normalize import alias_key

log = logging.getLogger("neuroscope.mechanisms")

API = "https://www.ebi.ac.uk/chembl/api/data"
PAUSE_S = 0.15


def _keys_by_drug(conn: psycopg.Connection) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for key, drug_id in trusted_keys(conn).items():
        out.setdefault(drug_id, set()).add(key)
    return out


def find_molecule(session, name: str, keys: set[str]) -> tuple[str, str] | None:
    """(parent ChEMBL ID, the matching ChEMBL name) or None."""
    resp = session.get(f"{API}/molecule/search.json", params={"q": name, "limit": 10}, timeout=30)
    resp.raise_for_status()
    for m in resp.json().get("molecules", []):
        names = [m.get("pref_name") or ""] + [s.get("molecule_synonym") or "" for s in m.get("molecule_synonyms") or []]
        hit = next((n for n in names if n and alias_key(n) in keys), None)
        if hit:
            parent = (m.get("molecule_hierarchy") or {}).get("parent_chembl_id") or m["molecule_chembl_id"]
            return parent, hit
    return None


def mechanisms_of(session, chembl_id: str, targets: dict[str, tuple[str, str]]) -> list[dict]:
    resp = session.get(f"{API}/mechanism.json", params={"parent_molecule_chembl_id": chembl_id, "limit": 50}, timeout=30)
    resp.raise_for_status()
    out = []
    for m in resp.json().get("mechanisms", []):
        if not m.get("mechanism_of_action"):
            continue
        tid = m.get("target_chembl_id")
        if tid and tid not in targets:
            t = session.get(f"{API}/target/{tid}.json", timeout=30)
            t.raise_for_status()
            targets[tid] = (t.json().get("pref_name"), t.json().get("target_type"))
            time.sleep(PAUSE_S)
        name, ttype = targets.get(tid, (None, None)) if tid else (None, None)
        out.append({"mechanism_of_action": m["mechanism_of_action"], "action_type": m.get("action_type"),
                    "target_chembl_id": tid, "target_name": name, "target_type": ttype})
    return out


def load(conn: psycopg.Connection, min_trials: int = 2, max_drugs: int | None = None,
         session=None) -> dict[str, int]:
    session = session or _session()
    drugs = conn.execute(
        "SELECT d.id, d.canonical_name FROM drugs d JOIN v_drug_pipeline v ON v.drug_id = d.id "
        "WHERE NOT d.is_class AND NOT d.is_combination AND v.n_trials >= %s "
        "ORDER BY v.n_trials DESC, d.canonical_name", (min_trials,)).fetchall()[:max_drugs]
    keys = _keys_by_drug(conn)
    conn.commit()                          # no read transaction held open while fetching
    targets: dict[str, tuple[str, str]] = {}
    summary = {"drugs_looked_up": 0, "drugs_matched": 0, "drugs_with_mechanism": 0, "mechanisms": 0, "errors": 0}
    for n, (drug_id, name) in enumerate(drugs, start=1):
        summary["drugs_looked_up"] += 1
        query = name.split("]", 1)[-1].strip() or name        # "[18F]florbetapir" -> "florbetapir"
        try:
            hit = find_molecule(session, query, keys.get(drug_id, set()))
            time.sleep(PAUSE_S)
            mechs = mechanisms_of(session, hit[0], targets) if hit else []
        except requests.RequestException as exc:
            summary["errors"] += 1
            log.warning("%s: skipped: %s", name, exc)
            continue
        with conn.transaction():
            conn.execute("DELETE FROM drug_mechanisms WHERE drug_id = %s", (drug_id,))
            for m in mechs:
                conn.execute(
                    "INSERT INTO drug_mechanisms (drug_id, chembl_id, matched_name, mechanism_of_action, action_type, "
                    "target_chembl_id, target_name, target_type) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (drug_id, hit[0], hit[1], m["mechanism_of_action"], m["action_type"], m["target_chembl_id"],
                     m["target_name"], m["target_type"]))
        summary["drugs_matched"] += bool(hit)
        summary["drugs_with_mechanism"] += bool(mechs)
        summary["mechanisms"] += len(mechs)
        if n % 50 == 0:
            log.info("%d/%d drugs looked up", n, len(drugs))
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("load")
    sp.add_argument("--min-trials", type=int, default=2)
    sp.add_argument("--max-drugs", type=int, default=None)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with connect() as conn:
        s = load(conn, args.min_trials, args.max_drugs)
    print(f"ChEMBL: {s['drugs_looked_up']} drugs looked up, {s['drugs_matched']} matched, "
          f"{s['drugs_with_mechanism']} with a mechanism ({s['mechanisms']} rows), {s['errors']} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
