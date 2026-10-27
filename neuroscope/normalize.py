"""Pure normalization functions: no database, no network.

Everything here is deterministic so it can be unit tested exhaustively.
Agents should never be asked to do this work; plain code is cheaper,
faster, and always gives the same answer.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

# ---------------------------------------------------------------------------
# Trial status
# ---------------------------------------------------------------------------

STATUS_GROUPS = {
    "RECRUITING": "active",
    "NOT_YET_RECRUITING": "active",
    "ACTIVE_NOT_RECRUITING": "active",
    "ENROLLING_BY_INVITATION": "active",
    "COMPLETED": "completed",
    "TERMINATED": "stopped",
    "WITHDRAWN": "stopped",
    "SUSPENDED": "stopped",
    "UNKNOWN": "unknown",
}


def status_group(overall_status: str | None) -> str:
    """Collapse CT.gov's ~15 statuses into five buckets agents can reason about."""
    if not overall_status:
        return "unknown"
    return STATUS_GROUPS.get(overall_status.upper(), "other")


# ---------------------------------------------------------------------------
# Trial phase
# ---------------------------------------------------------------------------

PHASE_VALUES = {
    "EARLY_PHASE1": 0.5,
    "PHASE1": 1.0,
    "PHASE2": 2.0,
    "PHASE3": 3.0,
    "PHASE4": 4.0,
}


def normalize_phases(phases: list[str] | None) -> tuple[str, float | None]:
    """Return (display label, highest phase as a number).

    ["PHASE2", "PHASE3"] -> ("Phase 2/3", 3.0)
    ["EARLY_PHASE1"]     -> ("Early Phase 1", 0.5)
    ["NA"] or []         -> ("Not applicable", None)
    """
    values = sorted({PHASE_VALUES[p] for p in (phases or []) if p in PHASE_VALUES})
    if not values:
        return "Not applicable", None
    if values == [0.5]:
        return "Early Phase 1", 0.5
    nums = [str(int(v)) for v in values if v >= 1]
    label = "Phase " + "/".join(nums)
    if 0.5 in values:
        label = "Early Phase 1/" + label
    return label, max(values)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_partial_date(value: str | None) -> tuple[date | None, str | None]:
    """CT.gov dates come as 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'.

    Returns (date, precision). A month-precision date is stored as the first
    of the month, and the precision column records that it is approximate.
    """
    if not value:
        return None, None
    parts = value.strip().split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2])), "day"
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1), "month"
        if len(parts) == 1:
            return date(int(parts[0]), 1, 1), "year"
    except ValueError:
        pass
    return None, None


# ---------------------------------------------------------------------------
# Drug names
# ---------------------------------------------------------------------------

# Intervention types that represent a drug-like entity worth tracking.
DRUG_INTERVENTION_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC"}

_PLACEBO_RE = re.compile(r"\b(placebo|sham|sugar pills?)\b", re.IGNORECASE)

# Strings that show up as names or otherNames but are not drug identities.
_JUNK_NAMES = {
    "drug", "study drug", "investigational product", "investigational drug", "ip",
    "active", "active drug", "active treatment", "treatment", "control",
    "standard of care", "soc", "usual care", "n/a", "na", "none", "other",
    "experimental", "study medication", "medication", "test drug",
    "no other name", "no other names", "not applicable", "human",
    "drug product", "botanical drug", "botanical drug product",
    "eeg", "mri", "fmri", "pet", "pet scan", "ct scan", "mri scan", "blood collection", "blood draw",
}

# Arm labels sponsors type into name fields: "Treatment A", "Active Comparator: B", "Group 2".
_ARM_LABEL_RE = re.compile(
    r"(?:active comparator|placebo comparator|experimental|treatment|arm|group|cohort|part|period)"
    r"\s*:?\s*[a-z0-9]?",
    re.IGNORECASE,
)

# Doses like "10 mg/kg", "1.5mg", "400 IU", "5%".
# The trailing (?!\w) rather than \b lets "10%)" match: % is not a word character.
_DOSE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|µg|μg|ug|g|ml|iu|units?|%)"
    r"(?:\s*/\s*(?:kg|day|d|ml|m2|dose|week|wk))?(?!\w)",
    re.IGNORECASE,
)

# Radiolabels: "[18F]", "(18F)", "18F-", "F 18", "[11C]", "C-11". The label is
# part of a tracer's identity and is never discarded: every notation of the
# same label is rewritten to one prefix form, so "18F-MK-6240", "[18F]MK6240"
# and "MK-6240 F 18" share a key. A labeled name never matches the unlabeled
# compound ("MK-6240"); that call is left to a human. A label alone
# ("Florbetapir (18F)") labels the name it is attached to and is never a name.
_ISOTOPE = (r"(?:18\s*-?\s*F|F\s*-?\s*18|11\s*-?\s*C|C\s*-?\s*11|123\s*-?\s*I|I\s*-?\s*123"
            r"|99m\s*-?\s*Tc|Tc\s*-?\s*99m|68\s*-?\s*Ga|Ga\s*-?\s*68|64\s*-?\s*Cu|89\s*-?\s*Zr)")
_ISOTOPE_RE = re.compile(r"\[\s*" + _ISOTOPE + r"\s*\]|(?<![\w-])" + _ISOTOPE + r"(?!\w)", re.IGNORECASE)


def _split_isotope(text: str) -> tuple[str, str | None]:
    """'18F-MK-6240' -> ('-MK-6240', '18F'). More than one label -> (text, None), untouched."""
    found = list(_ISOTOPE_RE.finditer(text))
    if len(found) != 1:
        return text, None
    compact = re.sub(r"[\[\]\s-]", "", found[0].group())
    mass = re.search(r"\d+m?", compact).group()
    element = compact.replace(mass, "", 1)
    return _ISOTOPE_RE.sub(" ", text), mass + element.capitalize()


def _with_label(name: str, label: str | None) -> str:
    return f"[{label}]{name}" if name and label else name

# Route, formulation, schedule, and arm words that are not part of a drug's identity.
_NOISE_WORDS = [
    r"i\.?v\.?", "intravenous(?:ly)?", "subcutaneous(?:ly)?", r"s\.?c\.?", "oral(?:ly)?",
    "tablets?", "capsules?", "injections?", "infusions?", "solution", "patch",
    "transdermal", "extended[- ]release", "immediate[- ]release", "er", "xr", "sr", "ir",
    "film[- ]coated", "pills?", "dispersible", "daily", "once", "twice", "weekly", "monthly", "biweekly",
    "bid", "tid", "qd", "qw", r"q\d+w", r"every \d+ weeks?", "low", "medium", "mid", "moderate", "high", "double",
    "dose", "doses", "dosing", "titration", "arm", "group", "cohort", "regimen",
    "auto[- ]?injector", "pen", "prefilled syringe", "pre-filled syringe", "vial", "formulation",
    "hydrochloride", "hcl", "mesylate", "maleate", "tartrate", "hydrobromide",
    "sulfate", "citrate", "besylate", "fumarate", "succinate", "sodium",
]
# Not when hyphenated to a code: the "ER" in "ER-176" is part of the code.
_NOISE_RE = re.compile(r"(?<![\w-])(?:" + "|".join(_NOISE_WORDS) + r")\b(?!-\w)", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^()]*)\)")


# Greek letters are spelled out so "Aβ" == "Abeta" and interferon alpha != beta.
_GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
          "κ": "kappa", "λ": "lambda", "μ": "mu", "ω": "omega", "τ": "tau"}


def alias_key(name: str) -> str:
    """Lookup key: NFKC-normalized, Greek spelled out, accents dropped, lowercase, alphanumerics only.

    'BAN-2401', 'BAN 2401', 'ban2401' -> 'ban2401'
    'Donépezil' -> 'donepezil'; 'Aβ vaccine' -> 'abetavaccine'
    """
    s = unicodedata.normalize("NFKC", name).lower()
    s = "".join(_GREEK.get(ch, ch) for ch in s)
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", s)


def is_placebo(name: str | None) -> bool:
    return bool(name and _PLACEBO_RE.search(name))


def _strip_noise(text: str) -> str:
    text = text.replace("®", "").replace("™", "")
    text = _DOSE_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    # Lone 1-2 digit numbers are arm or dose labels ("Donanemab Dose 1").
    # Longer ones are kept because they can be part of a code ("BAN 2401").
    text = re.sub(r"(?<![\w-])\d{1,2}(?![\w-])", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+,", ",", text)
    return text.strip(" -,;:/.")


def _name_code_pair(text: str) -> tuple[str, str] | None:
    """'Brexpiprazole, OPC-34712' -> ('Brexpiprazole', 'OPC-34712').

    Only a one-word letters-only name next to a code counts; any other
    comma list is treated as a combination by split_combination.
    """
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        return None
    for name, code in (parts, parts[::-1]):
        if (re.fullmatch(r"[A-Za-z][A-Za-z\-]{3,}", name) and is_usable_alias(name)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9 \-]*\d[A-Za-z0-9\-]*", code)
                and len(code.split()) <= 2):
            return name, code
    return None


def clean_drug_name(raw: str) -> tuple[str, list[str]]:
    """Split a registered intervention name into a main name and embedded aliases.

    Embedded aliases are names in parentheses, or a code after a comma.
    Radiolabels are kept, in one canonical prefix form (see _ISOTOPE).

    'Donanemab (LY3002813)'           -> ('Donanemab', ['LY3002813'])
    'Brexpiprazole, OPC-34712'        -> ('Brexpiprazole', ['OPC-34712'])
    'Lecanemab 10 mg/kg IV biweekly'  -> ('Lecanemab', [])
    'Donepezil Hydrochloride'         -> ('Donepezil', [])
    'Florbetapir (18F)'               -> ('[18F]Florbetapir', [])
    """
    main_text, main_label = _split_isotope(_PAREN_RE.sub(" ", raw))
    parens: list[str] = []
    for p in _PAREN_RE.findall(raw):
        p_text, p_label = _split_isotope(p)
        p_name = _strip_noise(p_text)
        if not p_name and p_label and not main_label:
            main_label = p_label          # "(18F)" labels the main name
        else:
            parens.append(_with_label(p_name, p_label))
    main = _strip_noise(main_text)
    pair = _name_code_pair(main)
    if pair:
        main = pair[0]
        parens.append(pair[1])
    return _with_label(main, main_label), [p for p in parens if p]


_OR_SPLIT_RE = re.compile(r"\s*\bor\b\s*", re.IGNORECASE)


def lists_alternatives(name: str) -> bool:
    """'18F-Florbetapir or 18F-Flutemetamol' names either drug, not one drug.

    True when splitting on 'or' leaves at least two usable names.
    """
    parts = [_strip_noise(p) for p in _OR_SPLIT_RE.split(_strip_noise(name))]
    return sum(1 for p in parts if is_usable_alias(p)) >= 2


def is_usable_alias(name: str) -> bool:
    """Reject empty strings, placebo labels, generic words, and lists of alternatives."""
    if not name or is_placebo(name):
        return False
    if _OR_SPLIT_RE.search(name) and lists_alternatives(name):
        return False
    if name.strip().lower() in _JUNK_NAMES or _ARM_LABEL_RE.fullmatch(name.strip()):
        return False
    if _ISOTOPE_RE.fullmatch(name.strip()):
        return False                      # a radiolabel alone names nothing
    key = alias_key(name)
    if len(key) < 3 or key.isdigit():
        return False
    return True


def classify_alias_type(name: str) -> str:
    """Codes contain digits (BAN2401, MK-8931); everything else is 'other' until curated."""
    return "code" if any(ch.isdigit() for ch in name) else "other"


# Separators that join ingredients. A bare "with" is deliberately excluded
# ("donepezil with food" is not a combination); "in combination with" is kept.
# A comma joins ingredients too ("Rivastigmine, memantine"), except in a
# name-code pair, which clean_drug_name has already split off.
_COMBO_SPLIT_RE = re.compile(
    r"\s*(?:\+|&|/|;|,|\bplus\b|\band\b|\bin combination with\b|\bcombined with\b)\s*",
    re.IGNORECASE,
)
# "donepezil, rivastigmine or galantamine" lists alternatives, not ingredients.
_ALTERNATIVES_RE = re.compile(r"\bor\b", re.IGNORECASE)


def split_combination(name: str) -> list[str] | None:
    """Return cleaned ingredient names if `name` is a combination, else None.

    'Donepezil + Memantine'            -> ['Donepezil', 'Memantine']
    'Memantine and Donepezil 10 mg'    -> ['Memantine', 'Donepezil']
    'Xanomeline/Trospium'              -> ['Xanomeline', 'Trospium']
    'BAN2401'                          -> None
    'Donepezil or Rivastigmine'        -> None (alternatives)
    Requires at least two usable ingredient names.
    """
    cleaned = _strip_noise(name)
    if _ALTERNATIVES_RE.search(cleaned):
        return None
    parts = [_strip_noise(p) for p in _COMBO_SPLIT_RE.split(cleaned)]
    parts = [p for p in parts if is_usable_alias(p)]
    return parts if len(parts) >= 2 else None


_BARE_PLACEBO_RE = re.compile(
    r"(?:matching |matched )?(?:placebo|sham)(?: (?:control|comparator))?", re.IGNORECASE)


def active_part_of_placebo_mix(name: str) -> str | None:
    """'DAOIB plus placebo' -> 'DAOIB'; 'Placebo + A + B' -> 'A + B'.

    Only a bare placebo ingredient is dropped. Anything that describes the
    placebo ('Placebo to match emtricitabine/tenofovir', 'X placebo; Y placebo')
    returns None, and the whole intervention stays a placebo.
    """
    if not is_placebo(name):
        return None
    parts = [_strip_noise(p) for p in _COMBO_SPLIT_RE.split(_strip_noise(name))]
    parts = [p for p in parts if p]
    bare = [p for p in parts if _BARE_PLACEBO_RE.fullmatch(p)]
    active = [p for p in parts if not is_placebo(p)]
    if not bare or not active or len(bare) + len(active) != len(parts):
        return None
    return " + ".join(active)


def combination_key(components: list[str]) -> str:
    """Order-independent key: 'A + B' and 'B and A' get the same key."""
    return "combo:" + "+".join(sorted(alias_key(c) for c in components))


def combination_display(components: list[str]) -> str:
    def fmt(c: str) -> str:
        return c.lower() if re.fullmatch(r"[A-Za-z][A-Za-z \-]*", c) else c
    return " + ".join(fmt(c) for c in sorted(components, key=alias_key))


def choose_display_name(candidates: list[str]) -> str:
    """Pick the canonical display name for a newly discovered drug.

    Prefer the registered name, but if it is a code and a one-word
    letters-only alternative exists (a likely nonproprietary name), use that
    instead. A multi-word phrase ("gamma secretase inhibitor") is usually a
    description and an all-caps word ("DN-TNF", "FBB") an abbreviation, so
    neither replaces a code; both are still kept as aliases.
    Letters-only names are lowercased to match INN convention.
    """
    usable = [c for c in candidates if is_usable_alias(c)]
    if not usable:
        raise ValueError("no usable candidate names")
    first = usable[0]
    unlabeled = re.sub(r"^\[[^\]]*\]", "", first)   # "[18F]Florbetapir" is a name, not a code
    if any(ch.isdigit() for ch in unlabeled):
        single_names = [c for c in usable
                        if re.fullmatch(r"[A-Za-z][A-Za-z\-]*", c) and not c.isupper()]
        if single_names:
            first = single_names[0]
    if re.fullmatch(r"[A-Za-z][A-Za-z \-]*", first):
        return first.lower()
    return first
