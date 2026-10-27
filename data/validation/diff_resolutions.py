"""Compare intervention resolutions between two databases loaded from the same studies.

Usage:
    python data/validation/diff_resolutions.py BEFORE_DB AFTER_DB > before_after.txt

Produced before_after_2026-10-27.txt: data/raw/ad_sample_1500_2026-10-27.jsonl loaded
once with the original resolver (live run) and once with the fixed one (replay).
"""
import collections
import sys

import psycopg

Q = ("SELECT ti.nct_id, ti.id, ti.name, d.canonical_name, ti.resolution_method FROM trial_interventions ti "
     "LEFT JOIN drugs d ON d.id = ti.drug_id ORDER BY ti.nct_id, ti.id")


def load(db):
    rows = psycopg.connect(f"postgresql://postgres:postgres@localhost:5432/{db}").execute(Q).fetchall()
    out, seen = {}, collections.Counter()
    for nct, _, name, drug, m in rows:
        seen[(nct, name)] += 1
        out[(nct, name, seen[(nct, name)])] = (drug, m)
    return out


b, a = load(sys.argv[1]), load(sys.argv[2])
assert b.keys() == a.keys(), "intervention rows differ"
changed = [(k, b[k], a[k]) for k in b if b[k] != a[k]]
print(f"{len(b)} interventions, original names identical in both; {len(changed)} resolutions changed")
print(collections.Counter((x[1][1], x[2][1]) for x in changed).most_common())
for (nct, name, _), (bd, bm), (ad, am) in sorted(changed, key=lambda x: (x[2][1], x[0])):
    print(f"{nct} | {name[:70]} | {bd} ({bm}) -> {ad} ({am})")
