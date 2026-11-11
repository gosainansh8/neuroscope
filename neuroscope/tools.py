"""Read-only lookups an agent can call. Each returns plain data with its sources.

These are ordinary functions over the database: no model, no network (except
literature search by meaning, which embeds the question). Everything an agent
is allowed to say must come from what these return, so each result carries
the identifiers needed to cite it (NCT IDs, FDA application numbers, PMIDs)
and the caveats that travel with the data.
"""

from __future__ import annotations

import psycopg

from .search import resolve_drug, search, semantic_search

TRIAL_URL = "https://clinicaltrials.gov/study/{}"


def _drug(conn: psycopg.Connection, name: str) -> tuple[int, str] | None:
    drug_id = resolve_drug(conn, name)
    if drug_id is None:
        return None
    return drug_id, conn.execute("SELECT canonical_name FROM drugs WHERE id = %s", (drug_id,)).fetchone()[0]


def _unknown(conn: psycopg.Connection, name: str) -> dict:
    like = f"%{name.strip()}%"
    similar = [r[0] for r in conn.execute(
        "SELECT canonical_name FROM drugs WHERE canonical_name ILIKE %s ORDER BY length(canonical_name) LIMIT 5",
        (like,))]
    return {"found": False, "name": name,
            "note": "No drug has this as a trusted name. Similar names are suggestions, not matches.",
            "similar_names": similar}


def find_drug(conn: psycopg.Connection, name: str) -> dict:
    """Resolve a name (generic, brand, or code) to the canonical drug and its verified names."""
    hit = _drug(conn, name)
    if not hit:
        return _unknown(conn, name)
    drug_id, canonical = hit
    mechanism, modality, is_combo = conn.execute(
        "SELECT mechanism, modality, is_combination FROM drugs WHERE id = %s", (drug_id,)).fetchone()
    names = [r[0] for r in conn.execute(
        "SELECT alias_display FROM drug_aliases WHERE drug_id = %s AND review_status = 'verified' ORDER BY 1",
        (drug_id,))]
    return {"found": True, "canonical_name": canonical, "verified_names": names, "mechanism": mechanism,
            "modality": modality, "is_combination": is_combo}


def get_trials(conn: psycopg.Connection, drug: str, limit: int = 8) -> dict:
    """Trial counts and the most relevant trials for a drug (active first, then highest phase)."""
    hit = _drug(conn, drug)
    if not hit:
        return _unknown(conn, drug)
    drug_id, canonical = hit
    summary = conn.execute(
        "SELECT n_trials, n_active_trials, highest_phase_any, highest_phase_active, latest_source_update::text "
        "FROM v_drug_pipeline WHERE drug_id = %s", (drug_id,)).fetchone()
    rows = conn.execute(
        """
        SELECT t.nct_id, t.brief_title, t.phase_label, t.overall_status, t.status_group, s.name,
               t.enrollment, t.primary_completion_date::text, t.last_update_posted::text, t.has_results
        FROM trials t LEFT JOIN sponsors s ON s.id = t.lead_sponsor_id
        WHERE t.study_type = 'INTERVENTIONAL'
          AND EXISTS (SELECT 1 FROM trial_interventions ti WHERE ti.nct_id = t.nct_id AND ti.drug_id = %s)
        ORDER BY (t.status_group = 'active') DESC, t.highest_phase DESC NULLS LAST,
                 t.last_update_posted DESC NULLS LAST, t.nct_id
        LIMIT %s
        """, (drug_id, limit)).fetchall()
    keys = ["nct_id", "title", "phase", "status", "status_group", "lead_sponsor", "enrollment",
            "primary_completion_date", "source_last_updated", "has_results"]
    trials = [dict(zip(keys, r), url=TRIAL_URL.format(r[0])) for r in rows]
    return {"found": True, "canonical_name": canonical,
            "summary": dict(zip(["n_trials", "n_active_trials", "highest_phase_any", "highest_phase_active",
                                 "latest_source_update"],
                                [float(v) if hasattr(v, "is_finite") else v for v in summary])) if summary else None,
            "trials_shown": len(trials), "trials": trials,
            "caveat": "Trial phase and status are not regulatory status. A completed Phase 3 or a Phase 4 "
                      "trial does not mean the drug is approved; use get_fda_status for approval."}


def get_fda_status(conn: psycopg.Connection, drug: str) -> dict:
    """FDA approval records and label indication text for a drug."""
    hit = _drug(conn, drug)
    if not hit:
        return _unknown(conn, drug)
    drug_id, canonical = hit
    row = conn.execute(
        "SELECT first_approval_date::text, n_new_drug_applications, n_generic_applications, currently_marketed, "
        "brand_names, source_export_date::text, n_labels, indications_mention_alzheimers "
        "FROM v_drug_fda_approval WHERE drug_id = %s", (drug_id,)).fetchone()
    if not row:
        return {"found": True, "canonical_name": canonical, "fda_approval_record": False,
                "caveat": "No matched FDA approval record. This is not proof the drug is unapproved: "
                          "records match by active-ingredient name, and only US FDA data is loaded."}
    apps = conn.execute(
        "SELECT DISTINCT a.application_number, a.application_type, a.sponsor_name, a.original_approval_date::text "
        "FROM fda_applications a JOIN fda_products p ON p.application_number = a.application_number "
        "WHERE p.drug_id = %s AND a.approval_status = 'approved' AND a.application_type IN ('NDA', 'BLA') "
        "ORDER BY 4 NULLS LAST LIMIT 6", (drug_id,)).fetchall()
    label = conn.execute(
        """
        SELECT l.brand_names, l.effective_date::text, l.application_numbers, left(l.indications_and_usage, 1200),
               l.indications_mention_alzheimers
        FROM fda_labels l WHERE l.application_numbers && ARRAY(
            SELECT a.application_number FROM fda_applications a JOIN fda_products p
              ON p.application_number = a.application_number
            WHERE p.drug_id = %s AND a.approval_status = 'approved')
        ORDER BY l.indications_mention_alzheimers DESC, l.effective_date DESC NULLS LAST LIMIT 1
        """, (drug_id,)).fetchone()
    return {
        "found": True, "canonical_name": canonical, "fda_approval_record": True,
        "first_approval_date": row[0], "brand_name_applications": row[1], "generic_applications": row[2],
        "currently_marketed": row[3], "brand_names": row[4], "fda_export_date": row[5],
        "applications": [dict(zip(["application_number", "type", "sponsor", "original_approval_date"], a))
                         for a in apps],
        "label_indications_mention_alzheimers": row[7],
        "label": dict(zip(["brand_names", "effective_date", "application_numbers", "indications_and_usage_text",
                           "mentions_alzheimers"], label)) if label else None,
        "caveat": "Approval here means approved by the US FDA for at least one use. Whether that use is "
                  "Alzheimer's disease is shown only by the label text: quote it, do not assume it. "
                  "label_indications_mention_alzheimers is a text search (null = no label retrieved).",
    }


def search_literature(conn: psycopg.Connection, query: str, drug: str | None = None, limit: int = 5,
                      embedder=None) -> dict:
    """PubMed abstracts matching a question. By meaning when an embedder is given, else by keyword."""
    if drug and not _drug(conn, drug):
        return _unknown(conn, drug)
    hits = (semantic_search(conn, embedder, query, drug, limit) if embedder
            else search(conn, query, drug, limit))
    return {
        "mode": "semantic" if embedder else "keyword", "query": query, "drug_filter": drug,
        "results": [{"pmid": (h.url or "").rstrip("/").rsplit("/", 1)[-1], "title": h.title, "journal": h.journal,
                     "published_date": h.published_date, "url": h.url, "linked_drugs": h.drugs[:8],
                     "excerpt": h.snippet.replace("**", "")} for h in hits],
        "caveat": "Abstracts only, for the most-studied drugs. Semantic results are the nearest texts, "
                  "relevant or not; read the excerpt before citing it.",
    }


def list_pipeline(conn: psycopg.Connection, min_phase: float = 3, active_only: bool = True,
                  limit: int = 25) -> dict:
    """Drugs by trial phase, with whether an FDA approval record exists."""
    column = "highest_phase_active" if active_only else "highest_phase_any"
    rows = conn.execute(
        f"""
        SELECT v.canonical_name, v.n_trials, v.n_active_trials, v.highest_phase_any, v.highest_phase_active,
               f.first_approval_date::text, f.indications_mention_alzheimers
        FROM v_drug_pipeline v LEFT JOIN v_drug_fda_approval f ON f.drug_id = v.drug_id
        WHERE v.{column} >= %s ORDER BY v.n_active_trials DESC, v.n_trials DESC, v.canonical_name LIMIT %s
        """, (min_phase, limit)).fetchall()
    keys = ["drug", "n_trials", "n_active_trials", "highest_phase_any", "highest_phase_active",
            "fda_first_approval_date", "fda_label_mentions_alzheimers"]
    return {"min_phase": min_phase, "active_only": active_only, "count": len(rows),
            "drugs": [dict(zip(keys, [float(v) if hasattr(v, "is_finite") else v for v in r])) for r in rows],
            "caveat": "Phases are trial phases. fda_first_approval_date is approval for any use; null means "
                      "no matched FDA record. Some rows are drug classes or comparators, not single drugs."}
