"""Read-only lookups an agent can call. Each returns plain data with its sources.

These are ordinary functions over the database: no model, no network (except
literature search by meaning, which embeds the question). Everything an agent
is allowed to say must come from what these return, so each result carries
the identifiers needed to cite it (NCT IDs, FDA application numbers, EMA product numbers, PMIDs)
and the caveats that travel with the data.
"""

from __future__ import annotations

import psycopg

from .search import hybrid_search, resolve_drug, search

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


DISEASE_ALIASES = {"alzheimer": "alzheimers", "ad": "alzheimers", "parkinson": "parkinsons", "pd": "parkinsons",
                   "als": "als", "amyotrophic lateral sclerosis": "als", "motor neuron disease": "als",
                   "lou gehrig": "als"}


def resolve_disease(conn: psycopg.Connection, value: str | None) -> str | None:
    """A disease slug from a slug or a plain name ("Parkinson's", "ALS"). ValueError if unknown."""
    if not value:
        return None
    text = value.strip().lower().replace("’", "'")
    slugs = [r[0] for r in conn.execute("SELECT slug FROM diseases ORDER BY sort_order")]
    if text in slugs:
        return text
    plain = text.replace("'s", "").replace(" disease", "").strip()
    if plain in DISEASE_ALIASES:
        return DISEASE_ALIASES[plain]
    for key, slug in DISEASE_ALIASES.items():
        if len(key) > 3 and key in plain:
            return slug
    raise ValueError(f"unknown disease {value!r}; use one of: {', '.join(slugs)}")


def _disease_info(conn: psycopg.Connection, slug: str) -> tuple[str, str]:
    return conn.execute("SELECT name, indication_pattern FROM diseases WHERE slug = %s", (slug,)).fetchone()


def _trials_by_disease(conn: psycopg.Connection, drug_id: int) -> dict[str, int]:
    return dict(conn.execute(
        "SELECT disease, n_trials FROM v_drug_disease_pipeline WHERE drug_id = %s ORDER BY disease",
        (drug_id,)).fetchall())


def find_drug(conn: psycopg.Connection, name: str) -> dict:
    """Resolve a name (generic, brand, or code) to the canonical drug and its verified names."""
    hit = _drug(conn, name)
    if not hit:
        return _unknown(conn, name)
    drug_id, canonical = hit
    mechanism, modality, is_combo, is_class = conn.execute(
        "SELECT mechanism, modality, is_combination, is_class FROM drugs WHERE id = %s", (drug_id,)).fetchone()
    names = [r[0] for r in conn.execute(
        "SELECT alias_display FROM drug_aliases WHERE drug_id = %s AND review_status = 'verified' ORDER BY 1",
        (drug_id,))]
    out = {"found": True, "canonical_name": canonical, "verified_names": names, "mechanism": mechanism,
           "modality": modality, "is_combination": is_combo, "is_class": is_class,
           "trials_by_disease": _trials_by_disease(conn, drug_id)}
    if is_class:
        out["note"] = ("This is a drug class registered as a trial intervention, not a single drug. "
                       "It has no FDA record of its own; ask about the specific drugs in it.")
    return out


def get_trials(conn: psycopg.Connection, drug: str, limit: int = 8, disease: str | None = None) -> dict:
    """Trial counts and the most relevant trials for a drug (active first, then highest phase),
    in one disease or all of them."""
    slug = resolve_disease(conn, disease)
    hit = _drug(conn, drug)
    if not hit:
        return _unknown(conn, drug)
    drug_id, canonical = hit
    summary = conn.execute(
        "SELECT n_trials, n_active_trials, highest_phase_any, highest_phase_active, latest_source_update::text "
        "FROM v_drug_disease_pipeline WHERE drug_id = %s AND disease = %s", (drug_id, slug)).fetchone() if slug \
        else conn.execute(
        "SELECT n_trials, n_active_trials, highest_phase_any, highest_phase_active, latest_source_update::text "
        "FROM v_drug_pipeline WHERE drug_id = %s", (drug_id,)).fetchone()
    rows = conn.execute(
        """
        SELECT t.nct_id, t.brief_title, t.phase_label, t.overall_status, t.status_group, s.name,
               t.enrollment, t.primary_completion_date::text, t.last_update_posted::text, t.has_results,
               ARRAY(SELECT td.disease FROM trial_diseases td WHERE td.nct_id = t.nct_id ORDER BY 1)
        FROM trials t LEFT JOIN sponsors s ON s.id = t.lead_sponsor_id
        WHERE t.study_type = 'INTERVENTIONAL'
          AND EXISTS (SELECT 1 FROM trial_interventions ti WHERE ti.nct_id = t.nct_id AND ti.drug_id = %s)
          AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM trial_diseases td WHERE td.nct_id = t.nct_id
                                           AND td.disease = %s))
        ORDER BY (t.status_group = 'active') DESC, t.highest_phase DESC NULLS LAST,
                 t.last_update_posted DESC NULLS LAST, t.nct_id
        LIMIT %s
        """, (drug_id, slug, slug, limit)).fetchall()
    keys = ["nct_id", "title", "phase", "status", "status_group", "lead_sponsor", "enrollment",
            "primary_completion_date", "source_last_updated", "has_results", "diseases"]
    trials = [dict(zip(keys, r), url=TRIAL_URL.format(r[0])) for r in rows]
    return {"found": True, "canonical_name": canonical, "disease_filter": slug,
            "trials_by_disease": _trials_by_disease(conn, drug_id),
            "summary": dict(zip(["n_trials", "n_active_trials", "highest_phase_any", "highest_phase_active",
                                 "latest_source_update"],
                                [float(v) if hasattr(v, "is_finite") else v for v in summary])) if summary else None,
            "trials_shown": len(trials), "trials": trials,
            "caveat": "Trial phase and status are not regulatory status. A completed Phase 3 or a Phase 4 "
                      "trial does not mean the drug is approved; use get_fda_status and get_ema_status for approval."}


_DRUG_LABELS = """
    SELECT l.set_id FROM fda_labels l WHERE l.application_numbers && ARRAY(
        SELECT a.application_number FROM fda_applications a JOIN fda_products p
          ON p.application_number = a.application_number
        WHERE p.drug_id = %s AND a.approval_status = 'approved')"""


def get_fda_status(conn: psycopg.Connection, drug: str, disease: str | None = None) -> dict:
    """FDA approval records and label indication text for a drug. With `disease`, the label shown
    is one whose indications mention that disease, if any does."""
    slug = resolve_disease(conn, disease) or "alzheimers"
    pattern = _disease_info(conn, slug)[1]
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
                          "records match by active-ingredient name. For the European Union, use get_ema_status."}
    apps = conn.execute(
        "SELECT DISTINCT a.application_number, a.application_type, a.sponsor_name, a.original_approval_date::text "
        "FROM fda_applications a JOIN fda_products p ON p.application_number = a.application_number "
        "WHERE p.drug_id = %s AND a.approval_status = 'approved' AND a.application_type IN ('NDA', 'BLA') "
        "ORDER BY 4 NULLS LAST LIMIT 6", (drug_id,)).fetchall()
    label = conn.execute(
        """
        SELECT l.brand_names, l.effective_date::text, l.application_numbers, left(l.indications_and_usage, 1200),
               COALESCE(l.indications_and_usage ~* %s, FALSE)
        FROM fda_labels l WHERE l.set_id IN (""" + _DRUG_LABELS + """)
        ORDER BY 5 DESC, l.effective_date DESC NULLS LAST LIMIT 1
        """, (pattern, drug_id)).fetchone()
    mentions = dict(conn.execute(
        "SELECT g.slug, COALESCE(BOOL_OR(l.indications_and_usage ~* g.indication_pattern), FALSE) "
        "FROM diseases g CROSS JOIN fda_labels l WHERE l.set_id IN (" + _DRUG_LABELS + ") "
        "GROUP BY g.slug", (drug_id,)).fetchall()) if label else {}
    return {
        "found": True, "canonical_name": canonical, "fda_approval_record": True,
        "first_approval_date": row[0], "brand_name_applications": row[1], "generic_applications": row[2],
        "currently_marketed": row[3], "brand_names": row[4], "fda_export_date": row[5],
        "applications": [dict(zip(["application_number", "type", "sponsor", "original_approval_date"], a))
                         for a in apps],
        "label_indications_mention": mentions or None,
        "label": dict(zip(["brand_names", "effective_date", "application_numbers", "indications_and_usage_text",
                           f"mentions_{slug}"], label)) if label else None,
        "caveat": "Approval here means approved by the US FDA for at least one use. Whether that use is "
                  "a given disease is shown only by the label text: quote it, do not assume it. "
                  "label_indications_mention is a text search by disease (null = no label retrieved).",
    }


EMA_NOTE = ("The EMA list covers only medicines authorised centrally for the whole EU. Many older drugs "
            "were authorised country by country instead, so no EMA record is not proof a drug is "
            "unavailable in Europe. Approval for a use is shown only by the indication text: quote it.")


def get_ema_status(conn: psycopg.Connection, drug: str, disease: str | None = None) -> dict:
    """European Medicines Agency records for a drug: authorised, refused, withdrawn, with indications.
    With `disease`, medicines whose indication mentions that disease are listed first."""
    slug = resolve_disease(conn, disease) or "alzheimers"
    pattern = _disease_info(conn, slug)[1]
    hit = _drug(conn, drug)
    if not hit:
        return _unknown(conn, drug)
    drug_id, canonical = hit
    rows = conn.execute(
        """
        SELECT ema_product_number, name, medicine_status, authorisation_date::text, refusal_date::text,
               application_withdrawal_date::text, withdrawal_date::text, marketing_authorisation_holder,
               conditional_approval, generic, left(therapeutic_indication, 900),
               COALESCE(concat_ws(' ', therapeutic_indication, therapeutic_area) ~* %s, FALSE),
               url, source_timestamp::date::text
        FROM ema_medicines WHERE drug_id = %s
        ORDER BY 12 DESC, (medicine_status = 'Authorised') DESC, generic, authorisation_date NULLS LAST, name
        LIMIT 8
        """, (pattern, drug_id)).fetchall()
    if not rows:
        return {"found": True, "canonical_name": canonical, "ema_record": False, "caveat": EMA_NOTE}
    keys = ["ema_product_number", "name", "status", "authorisation_date", "refusal_date",
            "application_withdrawn_date", "withdrawn_or_lapsed_date", "holder", "conditional_approval",
            "generic", "indication_text", f"indication_mentions_{slug}", "url"]
    medicines = [{k: v for k, v in zip(keys, r) if v not in (None, False)} for r in rows]
    total, authorised = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE medicine_status = 'Authorised') FROM ema_medicines "
        "WHERE drug_id = %s", (drug_id,)).fetchone()
    return {"found": True, "canonical_name": canonical, "ema_record": True,
            "currently_authorised_in_eu": authorised > 0, "n_medicines": total, "n_authorised": authorised,
            "medicines_shown": len(medicines), "medicines": medicines, "ema_file_date": rows[0][13],
            "caveat": EMA_NOTE}


def search_literature(conn: psycopg.Connection, query: str, drug: str | None = None, limit: int = 5,
                      embedder=None) -> dict:
    """PubMed abstracts matching a question: keyword and meaning combined when an embedder is
    given, keyword only otherwise. Excerpts are centred on the matching words."""
    if drug and not _drug(conn, drug):
        return _unknown(conn, drug)
    hits = (hybrid_search(conn, embedder, query, drug, limit) if embedder
            else search(conn, query, drug, limit))
    return {
        "mode": "hybrid" if embedder else "keyword", "query": query, "drug_filter": drug,
        "results": [{"pmid": (h.url or "").rstrip("/").rsplit("/", 1)[-1], "title": h.title, "journal": h.journal,
                     "published_date": h.published_date, "url": h.url, "linked_drugs": h.drugs[:8],
                     "excerpt": h.snippet.replace("**", "")} for h in hits],
        "caveat": "Abstracts only, for the most-studied drugs. Results are the best matches found, "
                  "relevant or not; read the excerpt before citing it.",
    }


def list_pipeline(conn: psycopg.Connection, min_phase: float = 3, active_only: bool = True,
                  limit: int = 25, disease: str | None = None) -> dict:
    """Drugs by trial phase in one disease or all, with whether US and EU approval records exist."""
    slug = resolve_disease(conn, disease)
    pattern = _disease_info(conn, slug or "alzheimers")[1]
    column = "highest_phase_active" if active_only else "highest_phase_any"
    source = "v_drug_disease_pipeline" if slug else "v_drug_pipeline"
    disease_clause, params = ("AND v.disease = %s", (slug,)) if slug else ("", ())
    order = ("v.n_active_trials DESC, v.n_trials DESC" if active_only
             else "v.n_trials DESC, v.n_active_trials DESC")
    rows = conn.execute(
        f"""
        SELECT v.canonical_name, v.n_trials, v.n_active_trials, v.highest_phase_any, v.highest_phase_active,
               f.first_approval_date::text,
               (SELECT BOOL_OR(l.indications_and_usage ~* %s) FROM fda_products p
                  JOIN fda_applications a ON a.application_number = p.application_number
                  JOIN fda_labels l ON a.application_number = ANY (l.application_numbers)
                 WHERE p.drug_id = v.drug_id AND a.approval_status = 'approved'),
               EXISTS (SELECT 1 FROM ema_medicines e WHERE e.drug_id = v.drug_id
                       AND e.medicine_status = 'Authorised')
        FROM {source} v LEFT JOIN v_drug_fda_approval f ON f.drug_id = v.drug_id
        WHERE v.{column} >= %s AND NOT v.is_class {disease_clause}
        ORDER BY {order}, v.canonical_name LIMIT %s
        """, (pattern, min_phase, *params, limit)).fetchall()
    label_key = f"fda_label_mentions_{slug or 'alzheimers'}"
    keys = ["drug", "n_trials", "n_active_trials", "highest_phase_any", "highest_phase_active",
            "fda_first_approval_date", label_key, "ema_authorised_medicine"]
    return {"min_phase": min_phase, "active_only": active_only, "disease_filter": slug, "count": len(rows),
            "drugs": [dict(zip(keys, [float(v) if hasattr(v, "is_finite") else v for v in r])) for r in rows],
            "caveat": "Phases are trial phases"
                      + (f" in {slug} trials" if slug else " across every disease loaded")
                      + ". fda_first_approval_date is approval for any use; null means no matched FDA record. "
                        "ema_authorised_medicine is a centrally authorised EU medicine for any use. "
                        "Drug classes are excluded; some rows may still be comparators."}
