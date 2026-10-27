"""Drug alias resolution: map a registered intervention to one canonical drug.

WHAT IS ALLOWED TO LINK TWO NAMES (the complete list):

  1. Exact key match against a TRUSTED alias. Keys are normalized strings
     ("BAN-2401" and "ban 2401" -> "ban2401"). There is no fuzzy matching,
     no partial matching, and no shared-word matching. Trusted means
     'verified', or 'unreviewed' but learned from this same NCT ID.

  2. Same-record equivalence. Names a sponsor lists together for ONE
     intervention in ONE registry record (the registered name, a name in
     parentheses, and that intervention's otherNames) are treated as the
     same drug. Aliases learned this way are stored as 'unreviewed', with
     the NCT ID that taught them, so a human can audit them.

UNREVIEWED ALIASES DO NOT RESOLVE OTHER TRIALS. An unreviewed alias learned
from trial A is a review candidate. If trial B's names hit it, B is left
unlinked with resolution_method 'pending_review' and matched_alias_key set to
that alias, and nothing is learned from B. Verifying the alias and running
`curate reresolve` links B. Set NEUROSCOPE_TRUST_UNREVIEWED_ALIASES=1 to
restore the older behavior where unreviewed aliases match everywhere.

WHAT NEVER LINKS NAMES: shared words, shared mechanism, shared sponsor,
appearing in the same trial (two interventions in one trial stay separate),
or string similarity.

ALTERNATIVES: a registered name like "A or B" says the arm got either drug.
It is left unlinked as 'ambiguous_name' and nothing is learned from it.

AMBIGUITY: if one intervention's names point at two different drugs, nothing
is merged and nothing new is learned from that record. If the registered
name itself matches, the intervention links to that drug; otherwise it stays
unlinked. Either way the disagreement goes to drug_alias_conflicts.

COMBINATIONS: 'Donepezil + Memantine' is its own drug with an
order-independent key. It never links to donepezil or memantine alone;
known ingredients are recorded in drug_components instead. A single-drug
intervention ignores combination-looking otherNames, and a combination
intervention ignores otherNames that name a single known drug.

The resolver keeps no cache. Every lookup runs inside the caller's
transaction, so a rolled-back trial leaves no orphan drugs or aliases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .normalize import (
    DRUG_INTERVENTION_TYPES,
    active_part_of_placebo_mix,
    lists_alternatives,
    alias_key,
    choose_display_name,
    classify_alias_type,
    clean_drug_name,
    combination_display,
    combination_key,
    is_placebo,
    is_usable_alias,
    split_combination,
)


@dataclass
class Resolution:
    drug_id: int | None
    method: str                      # matched | created | conflict | pending_review | ambiguous_name |
                                     # placebo | non_drug | no_usable_name
    matched_key: str | None = None
    created: bool = False
    conflicts: list[int] = field(default_factory=list)


@dataclass
class Candidate:
    key: str
    display: str
    source: str           # ctgov_name | ctgov_parenthetical | ctgov_other_name
    is_combination: bool
    components: list[str] | None = None


def _make_candidate(text: str, source: str) -> Candidate | None:
    comps = split_combination(text)
    if comps:
        return Candidate(combination_key(comps), combination_display(comps), source, True, comps)
    if not is_usable_alias(text):
        return None
    return Candidate(alias_key(text), text, source, False)


def build_candidates(name: str, other_names: list[str]) -> list[Candidate]:
    """Ordered, de-duplicated candidates. The first one is the registered name."""
    main, parens = clean_drug_name(name)
    raw: list[tuple[str, str]] = [(main, "ctgov_name")]
    raw += [(p, "ctgov_parenthetical") for p in parens]
    for other in other_names or []:
        o_main, o_parens = clean_drug_name(other)
        raw.append((o_main, "ctgov_other_name"))
        raw += [(p, "ctgov_other_name") for p in o_parens]

    out: list[Candidate] = []
    seen: set[str] = set()
    for text, source in raw:
        cand = _make_candidate(text, source)
        if cand and cand.key not in seen:
            seen.add(cand.key)
            out.append(cand)
    if not out:
        return out

    # Keep only candidates with the same combination-ness as the registered name
    # (or the first usable name, if the registered name was unusable).
    primary_is_combo = out[0].is_combination
    filtered = [c for c in out if c.is_combination == primary_is_combo]
    if primary_is_combo:
        component_keys = {alias_key(x) for c in filtered for x in (c.components or [])}
        # A plain otherName equal to an ingredient is that ingredient, not the combo.
        filtered += [c for c in out if not c.is_combination and c.key not in component_keys]
    return filtered


def trust_unreviewed_default() -> bool:
    return os.environ.get("NEUROSCOPE_TRUST_UNREVIEWED_ALIASES") == "1"


def resolve_intervention(cur, *, nct_id: str, name: str, intervention_type: str | None,
                         other_names: list[str], trust_unreviewed: bool | None = None) -> Resolution:
    if trust_unreviewed is None:
        trust_unreviewed = trust_unreviewed_default()
    registered_name = name           # conflicts are logged under the name as published
    if is_placebo(name):
        # "Drug X plus placebo" is an active arm; resolve the drug part.
        active = active_part_of_placebo_mix(name)
        if active is None:
            return Resolution(None, "placebo")
        name = active
    if (intervention_type or "").upper() not in DRUG_INTERVENTION_TYPES:
        return Resolution(None, "non_drug")
    if lists_alternatives(clean_drug_name(name)[0]):
        # "A or B": the record does not say which drug was given. Link nothing,
        # learn nothing; `curate ambiguous` lists these for review.
        return Resolution(None, "ambiguous_name")

    cands = build_candidates(name, other_names)
    if not cands:
        return Resolution(None, "no_usable_name")
    is_combo = cands[0].is_combination
    keys = [c.key for c in cands]
    primary_key = keys[0] if cands[0].source == "ctgov_name" else None

    cur.execute(
        "SELECT a.alias_key, a.drug_id, d.is_combination, a.review_status, a.first_seen_nct "
        "FROM drug_aliases a LEFT JOIN drugs d ON d.id = a.drug_id "
        "WHERE a.alias_key = ANY(%s)",
        (keys,),
    )
    owned: set[str] = set()       # keys that exist in the table in any state
    matched: dict[str, int] = {}  # trusted hits: may link this intervention
    pending: dict[str, int] = {}  # unreviewed hits learned from other trials: review candidates only
    for key, drug_id, drug_is_combo, status, first_nct in cur.fetchall():
        owned.add(key)
        if status == "rejected":
            continue
        if is_combo and not drug_is_combo:
            continue              # combination intervention never links to a single drug
        if status == "unreviewed" and first_nct != nct_id and not trust_unreviewed:
            pending[key] = drug_id
        else:
            matched[key] = drug_id

    # Pending hits still count as disagreement, so a conflict is logged for review.
    hits = {**pending, **matched}
    distinct = list(dict.fromkeys(hits[k] for k in keys if k in hits))

    if len(distinct) > 1:
        assigned = matched.get(primary_key) if primary_key else None
        conflicts = [d for d in distinct if d != assigned]
        for key in keys:
            other = hits.get(key)
            if other is None or other == assigned:
                continue
            cur.execute(
                "INSERT INTO drug_alias_conflicts "
                "(nct_id, intervention_name, alias_key, assigned_drug_id, conflicting_drug_id) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (nct_id, registered_name, key, assigned, other),
            )
        # Learn nothing from an ambiguous record.
        return Resolution(assigned, "conflict", matched_key=primary_key if assigned else None,
                          conflicts=conflicts)

    if distinct and not matched:
        # Only unreviewed aliases from other trials point here. Record which
        # one, learn nothing, and wait for a human to verify it.
        pending_key = next(k for k in keys if k in pending)
        return Resolution(None, "pending_review", matched_key=pending_key)

    created = False
    if distinct:
        drug_id = distinct[0]
        matched_key = next(k for k in keys if k in matched)
        method = "matched"
    else:
        lead = cands[0]
        canonical = lead.display if is_combo else choose_display_name([c.display for c in cands])
        cur.execute(
            "INSERT INTO drugs (canonical_name, is_combination) VALUES (%s, %s) "
            "ON CONFLICT (canonical_name) DO NOTHING RETURNING id",
            (canonical, is_combo),
        )
        row = cur.fetchone()
        if row:
            drug_id, created = row[0], True
        else:
            # Same display name already exists under a different key; reuse it
            # only if its combination-ness agrees, otherwise disambiguate.
            cur.execute("SELECT id, is_combination FROM drugs WHERE canonical_name = %s", (canonical,))
            existing_id, existing_combo = cur.fetchone()
            if existing_combo == is_combo:
                drug_id = existing_id
            else:
                cur.execute(
                    "INSERT INTO drugs (canonical_name, is_combination) VALUES (%s, %s) RETURNING id",
                    (f"{canonical} [{nct_id}]", is_combo),
                )
                drug_id, created = cur.fetchone()[0], True
        matched_key = None
        method = "created"

    # Register keys nobody owns yet (including rejected tombstones) as unreviewed.
    for c in cands:
        if c.key in owned:
            continue
        cur.execute(
            "INSERT INTO drug_aliases (alias_key, alias_display, drug_id, alias_type, source, "
            "review_status, first_seen_nct) VALUES (%s, %s, %s, %s, %s, 'unreviewed', %s) "
            "ON CONFLICT (alias_key) DO NOTHING",
            (c.key, c.display, drug_id,
             "combination" if c.is_combination else classify_alias_type(c.display),
             c.source, nct_id),
        )

    if is_combo:
        _link_components(cur, drug_id, cands[0].components or [], nct_id, trust_unreviewed)

    return Resolution(drug_id, method, matched_key=matched_key, created=created)


def _link_components(cur, combo_drug_id: int, components: list[str], nct_id: str,
                     trust_unreviewed: bool) -> None:
    """Record known ingredients of a combination. Never creates ingredient drugs.

    Uses the same trust rule as matching: verified aliases, or unreviewed ones
    learned from this trial.
    """
    keys = [alias_key(c) for c in components]
    cur.execute(
        "SELECT DISTINCT a.drug_id FROM drug_aliases a JOIN drugs d ON d.id = a.drug_id "
        "WHERE a.alias_key = ANY(%s) AND NOT d.is_combination AND (a.review_status = 'verified' "
        "OR (a.review_status = 'unreviewed' AND (a.first_seen_nct = %s OR %s)))",
        (keys, nct_id, trust_unreviewed),
    )
    for (component_id,) in cur.fetchall():
        cur.execute(
            "INSERT INTO drug_components (drug_id, component_drug_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (combo_drug_id, component_id),
        )
