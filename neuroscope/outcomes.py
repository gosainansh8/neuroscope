"""Standard clinical endpoints and biomarkers named in trial outcome measures.

Usage:
    python -m neuroscope.outcomes classify        # rebuild trial_outcome_terms from trial_outcomes

Each registered outcome measure (and its description) is matched against a fixed
vocabulary of the scales and biomarkers used in Alzheimer's, Parkinson's, and ALS
trials. A match is a whole-phrase text match, so "MDS-UPDRS Part III" counts as
MDS-UPDRS and "CSF neurofilament light" counts as NfL. An outcome can name several
terms. Measures that name none are simply not counted: the vocabulary is a lens for
comparison, not a full coding of every outcome.
"""

from __future__ import annotations

import re
import sys

import psycopg

from .db import connect

# slug, label, category, pattern (case-insensitive)
TERMS: list[tuple[str, str, str, str]] = [
    # Alzheimer's and cognition
    ("cdr_sb", "CDR Sum of Boxes", "clinical", r"\bCDR[\s-]*SB\b|sum of boxes"),
    ("cdr_global", "CDR global score", "clinical", r"\bCDR[\s-]*(global|GS)\b|clinical dementia rating(?!.*sum of boxes)"),
    ("adas_cog", "ADAS-Cog", "clinical", r"\bADAS[\s-]*cog|alzheimer'?s disease assessment scale"),
    ("mmse", "MMSE", "clinical", r"\bMMSE\b|mini[\s-]*mental state"),
    ("moca", "MoCA", "clinical", r"\bMoCA\b|montreal cognitive assessment"),
    ("adcs_adl", "ADCS-ADL", "clinical", r"\bADCS[\s-]*ADL"),
    ("iadrs", "iADRS", "clinical", r"\biADRS\b|integrated alzheimer'?s disease rating scale"),
    ("npi", "Neuropsychiatric Inventory", "clinical", r"\bNPI\b|neuropsychiatric inventory"),
    ("adcs_cgic", "Clinical global impression of change", "clinical", r"\bCGI[\s-]*(C|I|S)\b|ADCS[\s-]*CGIC|clinical global impression"),
    # Parkinson's
    ("mds_updrs", "MDS-UPDRS / UPDRS", "clinical", r"\b(MDS[\s-]*)?UPDRS\b|unified parkinson'?s disease rating scale"),
    ("off_time", "OFF / ON time (motor diaries)", "clinical", r"\boff[\s-]*(time|state|period|episode)|good on[\s-]*time|on[\s-]*time without|hauser|motor fluctuation diar"),
    ("pdq39", "PDQ-39 quality of life", "clinical", r"\bPDQ[\s-]*(39|8)\b|parkinson'?s disease questionnaire"),
    ("hoehn_yahr", "Hoehn and Yahr stage", "clinical", r"hoehn\s*(and|&)?\s*yahr"),
    ("dyskinesia", "Dyskinesia scales (UDysRS, AIMS)", "clinical", r"\bUDysRS\b|(?-i:\bAIMS\b)|dyskinesia rating"),
    # ALS
    ("alsfrs_r", "ALSFRS-R", "clinical", r"\bALSFRS"),
    ("vital_capacity", "Vital capacity (SVC / FVC)", "clinical", r"\b(S|F)VC\b|vital capacity"),
    ("survival", "Survival / time to death or ventilation", "clinical", r"\bsurvival\b|time to (death|tracheostomy|permanent (assisted )?ventilation)|ventilation[\s-]*free"),
    ("cafs", "CAFS (combined function and survival)", "clinical", r"\bCAFS\b|combined assessment of function and survival"),
    ("muscle_strength", "Muscle strength (HHD, ATLIS)", "clinical", r"hand[\s-]*held dynamometr|\bHHD\b|\bATLIS\b|muscle strength"),
    # Biomarkers
    ("amyloid_pet", "Amyloid PET", "biomarker", r"amyloid[^.;]{0,40}\bPET\b|\bPET\b[^.;]{0,40}amyloid|centiloid|florbetapir|florbetaben|flutemetamol|\bPiB\b"),
    ("tau_pet", "Tau PET", "biomarker", r"tau[^.;]{0,30}\bPET\b|\bPET\b[^.;]{0,30}tau|flortaucipir|MK[\s-]*6240|PI[\s-]*2620"),
    ("csf_amyloid", "CSF amyloid-beta", "biomarker", r"\bCSF\b[^.;]{0,40}(a[\s-]*beta|amyloid|Aβ)|(a[\s-]*beta|amyloid|Aβ)\s*(1-)?4[02][^.;]{0,30}\bCSF\b"),
    ("ptau", "Phosphorylated tau (CSF or plasma)", "biomarker", r"\bp[\s-]*tau|phospho[\s-]*tau|phosphorylated tau"),
    ("nfl", "Neurofilament light (NfL)", "biomarker", r"\bNfL\b|neurofilament"),
    ("gfap", "GFAP", "biomarker", r"\bGFAP\b|glial fibrillary"),
    ("dat_spect", "Dopamine transporter imaging (DaT-SPECT)", "biomarker", r"(?-i:\bDaT\b)|DaTscan|dopamine transporter|ioflupane|\bFP[\s-]*CIT\b|β[\s-]*CIT|beta[\s-]*CIT"),
    ("alpha_synuclein", "Alpha-synuclein (incl. seed amplification)", "biomarker", r"(alpha|α)[\s-]*synuclein|seed amplification|\bSAA\b"),
    ("sod1", "SOD1 protein", "biomarker", r"\bSOD1\b|superoxide dismutase"),
    ("mri_volume", "MRI brain volume / atrophy", "biomarker", r"(brain|hippocampal|whole[\s-]*brain|ventricular|cortical)[^.;]{0,20}(volume|atrophy)|(brain|cerebral|hippocampal|cortical) atrophy"),
    ("fdg_pet", "FDG-PET", "biomarker", r"\bFDG\b|fluorodeoxyglucose"),
    # Safety and drug levels
    ("aria", "ARIA (amyloid-related imaging abnormalities)", "safety", r"\bARIA\b|amyloid[\s-]*related imaging abnormalit"),
    ("adverse_events", "Adverse events", "safety", r"adverse event|(?-i:\bT?EAEs?\b|\bAEs?\b)|side effects?|safety and tolerability"),
    ("pharmacokinetics", "Pharmacokinetics", "safety", r"pharmacokinetic|\bC\s*max\b|\bAUC\b|half[\s-]*life|plasma concentration"),
]

_COMPILED = [(slug, cat, re.compile(pat, re.IGNORECASE)) for slug, _, cat, pat in TERMS]
LABELS = {slug: label for slug, label, _, _ in TERMS}
CATEGORY = {slug: cat for slug, _, cat, _ in TERMS}


def terms_in(text: str) -> list[str]:
    """The vocabulary terms named in one outcome's text."""
    return [slug for slug, _, rx in _COMPILED if rx.search(text or "")]


def classify(conn: psycopg.Connection) -> dict[str, int]:
    """Rebuild trial_outcome_terms. One transaction."""
    rows = conn.execute("SELECT nct_id, kind, measure, COALESCE(description, '') FROM trial_outcomes").fetchall()
    found = {(nct, kind, slug) for nct, kind, measure, desc in rows for slug in terms_in(f"{measure} {desc}")}
    with conn.transaction():
        conn.execute("DELETE FROM trial_outcome_terms")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO trial_outcome_terms (nct_id, kind, term, category) VALUES (%s, %s, %s, %s)",
                            [(n, k, s, CATEGORY[s]) for n, k, s in sorted(found)])
    return {"outcomes_read": len(rows), "terms_stored": len(found),
            "trials_with_a_term": len({n for n, _, _ in found})}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["classify"]:
        print(__doc__)
        return 2
    with connect() as conn:
        s = classify(conn)
    print(f"Outcomes: {s['outcomes_read']} read, {s['terms_stored']} terms stored across "
          f"{s['trials_with_a_term']} trials")
    return 0


if __name__ == "__main__":
    sys.exit(main())
