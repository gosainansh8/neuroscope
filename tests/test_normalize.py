from datetime import date

import pytest

from neuroscope.ctgov import content_hash, parse_study
from neuroscope.normalize import (
    active_part_of_placebo_mix,
    alias_key,
    choose_display_name,
    clean_drug_name,
    is_placebo,
    is_usable_alias,
    lists_alternatives,
    normalize_phases,
    parse_partial_date,
    split_combination,
    status_group,
)
from tests.fixtures import iv, study


@pytest.mark.parametrize("raw,expected", [
    ("BAN2401", "ban2401"),
    ("BAN-2401", "ban2401"),
    ("ban 2401", "ban2401"),
    ("ANAVEX 2-73", "anavex273"),
    ("Leqembi®", "leqembi"),
    # Live strings: accents and Greek letters are spelled out, never dropped.
    ("ARICEPT Donépezil", "ariceptdonepezil"),
    ("Aβ vaccine", "abetavaccine"),
    ("Interferon β-1a", "interferonbeta1a"),
    ("Interferon α-2a", "interferonalpha2a"),
])
def test_alias_key(raw, expected):
    assert alias_key(raw) == expected


@pytest.mark.parametrize("raw,main,parens", [
    ("Donanemab (LY3002813)", "Donanemab", ["LY3002813"]),
    ("Lecanemab 10 mg/kg IV biweekly", "Lecanemab", []),
    ("Donepezil Hydrochloride 10 mg", "Donepezil", []),
    ("Memantine extended-release 28 mg once daily", "Memantine", []),
    ("XYZ1234 oral tablet", "XYZ1234", []),
    ("E2020", "E2020", []),
    ("Semaglutide 14 mg/day", "Semaglutide", []),
    ("Lecanemab Subcutaneous Autoinjector", "Lecanemab", []),
    ("Donanemab Dose 1", "Donanemab", []),
    ("BAN 2401", "BAN 2401", []),
    ("ANAVEX 2-73", "ANAVEX 2-73", []),
    # Exact live strings that were mis-normalized before.
    ("Brexpiprazole, OPC-34712", "Brexpiprazole", ["OPC-34712"]),
    ("JNJ-54861911, 10 mg", "JNJ-54861911", []),
    ("Lu AF20513, double high dose", "Lu AF20513", []),
    ("Immune Globulin Intravenous (Human), 10% (IGIV, 10%) 200 mg/kg", "Immune Globulin", ["Human", "IGIV"]),
    ("Escitalopram pill", "Escitalopram", []),
    ("Ginkgo biloba dispersible tablets", "Ginkgo biloba", []),
    # Radiolabels are kept, in one notation.
    ("Florbetapir (18F)", "[18F]Florbetapir", []),
    ("Florbetapir F 18", "[18F]Florbetapir", []),
    ("AH110690 (18F) Injection", "[18F]AH110690", []),
    ("18F-MK-6240", "[18F]MK-6240", []),
    ("[18F]MK6240", "[18F]MK6240", []),
    ("C-11 PiB", "[11C]PiB", []),
    ("11C-PIB", "[11C]PIB", []),
    ("[18F]MNI-815 (MNI-815)", "[18F]MNI-815", ["MNI-815"]),
    ("C-11 ER-176", "[11C]ER-176", []),
    ("11C-ER176", "[11C]ER176", []),
    # Two labels: ambiguous, so the name is left exactly as cleaned.
    ("18F-Florbetapir or 18F-Flutemetamol", "18F-Florbetapir or 18F-Flutemetamol", []),
    ("Memantine ER", "Memantine", []),
    # Formulation words outside the dosage-form list are kept.
    ("Amyvid PET Scan", "Amyvid PET Scan", []),
])
def test_clean_drug_name(raw, main, parens):
    assert clean_drug_name(raw) == (main, parens)


@pytest.mark.parametrize("name,expected", [
    ("Placebo", True), ("Matching placebo", True), ("Placebo for lecanemab", True),
    ("Sham stimulation", True), ("Lecanemab", False), ("Sugar Pill", True),
])
def test_is_placebo(name, expected):
    assert is_placebo(name) is expected


@pytest.mark.parametrize("name,ok", [
    ("BAN2401", True), ("Study drug", False), ("placebo", False),
    ("IP", False), ("", False), ("123", False),
])
def test_is_usable_alias(name, ok):
    assert is_usable_alias(name) is ok


def test_choose_display_prefers_letters_only_name_over_code():
    assert choose_display_name(["XYZ-1234", "fakeumab"]) == "fakeumab"
    assert choose_display_name(["XYZ-1234"]) == "XYZ-1234"
    assert choose_display_name(["Lecanemab", "BAN2401"]) == "lecanemab"


@pytest.mark.parametrize("phases,label,highest", [
    (["PHASE3"], "Phase 3", 3.0),
    (["PHASE2", "PHASE3"], "Phase 2/3", 3.0),
    (["PHASE1", "PHASE2"], "Phase 1/2", 2.0),
    (["EARLY_PHASE1"], "Early Phase 1", 0.5),
    (["NA"], "Not applicable", None),
    ([], "Not applicable", None),
])
def test_normalize_phases(phases, label, highest):
    assert normalize_phases(phases) == (label, highest)


@pytest.mark.parametrize("status,group", [
    ("RECRUITING", "active"), ("ACTIVE_NOT_RECRUITING", "active"),
    ("COMPLETED", "completed"), ("TERMINATED", "stopped"), ("WITHDRAWN", "stopped"),
    ("UNKNOWN", "unknown"), ("APPROVED_FOR_MARKETING", "other"), (None, "unknown"),
])
def test_status_group(status, group):
    assert status_group(status) == group


@pytest.mark.parametrize("value,expected", [
    ("2024-03-15", (date(2024, 3, 15), "day")),
    ("2024-03", (date(2024, 3, 1), "month")),
    ("2024", (date(2024, 1, 1), "year")),
    (None, (None, None)),
    ("garbage", (None, None)),
])
def test_parse_partial_date(value, expected):
    assert parse_partial_date(value) == expected


def test_parse_study_fields():
    rec = parse_study(study("NCT90000002", phases=["PHASE2", "PHASE3"], status="COMPLETED",
                            has_results=True, interventions=[iv("Donanemab (LY3002813)", "BIOLOGICAL")]))
    assert rec.nct_id == "NCT90000002"
    assert rec.phase_label == "Phase 2/3" and rec.highest_phase == 3.0
    assert rec.status_group == "completed" and rec.has_results
    assert rec.start_date == date(2024, 3, 1) and rec.start_date_precision == "month"
    assert rec.lead_sponsor == ("Test Pharma Inc.", "INDUSTRY")
    assert rec.interventions[0].name == "Donanemab (LY3002813)"
    assert [o.kind for o in rec.outcomes] == ["primary", "secondary"]


def test_parse_study_requires_nct_id():
    with pytest.raises(ValueError):
        parse_study({"protocolSection": {}})


def test_content_hash_ignores_derived_section():
    a = study("NCT90000001", version_holder="2026-10-01")
    b = study("NCT90000001", version_holder="2026-10-20")
    c = study("NCT90000001", status="COMPLETED")
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(c)


# --- Combinations ----------------------------------------------------------

from neuroscope.normalize import combination_display, combination_key, split_combination  # noqa: E402


@pytest.mark.parametrize("name,parts", [
    ("Donepezil + Memantine", ["Donepezil", "Memantine"]),
    ("Memantine and Donepezil 10 mg", ["Memantine", "Donepezil"]),
    ("Xanomeline/Trospium", ["Xanomeline", "Trospium"]),
    ("Lecanemab in combination with E2814", ["Lecanemab", "E2814"]),
    ("Lecanemab 10 mg/kg IV", None),          # mg/kg is a dose, not a combination
    ("BAN2401", None),
    ("Donepezil with food", None),            # bare 'with' is not a combination
    ("ANAVEX2-73", None),                     # hyphens in codes are not separators
])
def test_split_combination(name, parts):
    assert split_combination(name) == parts


def test_combination_key_is_order_independent():
    assert combination_key(["Donepezil", "Memantine"]) == combination_key(["memantine", "DONEPEZIL"])
    assert combination_display(["Memantine", "Donepezil"]) == "donepezil + memantine"


# --- Paging and missing fields ----------------------------------------------

from neuroscope.ctgov import iter_studies  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Serves 3 pages of 2 studies and records every request's params."""

    def __init__(self):
        self.calls = []
        self.pages = {
            None: {"totalCount": 5, "nextPageToken": "t2", "studies": [study("NCT1"), study("NCT2")]},
            "t2": {"nextPageToken": "t3", "studies": [study("NCT3"), study("NCT4")]},
            "t3": {"studies": [study("NCT5")]},
        }

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        return FakeResponse(self.pages[params.get("pageToken")])


def _ids(studies):
    return [s["protocolSection"]["identificationModule"]["nctId"] for s in studies]


def test_pagination_follows_tokens_to_the_end():
    sess, stats = FakeSession(), {}
    got = list(iter_studies("Alzheimer Disease", page_size=2, session=sess, pause_s=0, stats=stats))
    assert _ids(got) == ["NCT1", "NCT2", "NCT3", "NCT4", "NCT5"]
    assert stats == {"pages": 3, "total_count": 5}
    assert [c.get("pageToken") for c in sess.calls] == [None, "t2", "t3"]
    assert "countTotal" in sess.calls[0] and "countTotal" not in sess.calls[1]


def test_pagination_stops_at_limit_without_extra_requests():
    sess = FakeSession()
    got = list(iter_studies("Alzheimer Disease", page_size=2, max_studies=3, session=sess, pause_s=0))
    assert _ids(got) == ["NCT1", "NCT2", "NCT3"]
    assert len(sess.calls) == 2


def test_fetch_by_ids_uses_filter():
    sess = FakeSession()
    list(iter_studies(nct_ids=["NCT1", "NCT2"], session=sess, pause_s=0))
    assert sess.calls[0]["filter.ids"] == "NCT1,NCT2" and "query.cond" not in sess.calls[0]


def test_parse_study_with_only_required_fields():
    rec = parse_study({"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}})
    assert rec.brief_title == "NCT00000001"
    assert rec.overall_status == "UNKNOWN" and rec.status_group == "unknown"
    assert rec.phase_label == "Not applicable" and rec.highest_phase is None
    assert rec.enrollment is None and rec.start_date is None and rec.lead_sponsor is None
    assert rec.interventions == [] and rec.outcomes == []


@pytest.mark.parametrize("name,active", [
    ("DAOIB plus placebo", "DAOIB"),
    ("Placebo and S-equol", "S-equol"),
    ("Donepezil + Memantine + matching placebo", "Donepezil + Memantine"),
    ("Placebo", None),
    ("Placebo to Match Emtricitabine 200mg/Tenofovir Alafenamide 25 mg.", None),
    ("Huperzine A controlled-release tablets placebo; Donepezil hydrochloride tablets placebo", None),
    ("Sham self-administered online CBT plus therapist check in", None),
])
def test_active_part_of_placebo_mix(name, active):
    assert active_part_of_placebo_mix(name) == active


@pytest.mark.parametrize("name,parts", [
    ("Rivastigmine, memantine", ["Rivastigmine", "memantine"]),
    ("Risperdal,reminyl", ["Risperdal", "reminyl"]),
    ("Vitamin E, Vitamin C, and Alpha-lipoic Acid", ["Vitamin E", "Vitamin C", "Alpha-lipoic Acid"]),
    # Alternatives are not a combination.
    ("Conventional medical treatment：donepezil, rivastigmine or galantamine", None),
])
def test_split_combination_live_strings(name, parts):
    assert split_combination(name) == parts


@pytest.mark.parametrize("name", ["Treatment A", "Active Comparator: A", "no other names", "Human",
                                  "EEG", "PET scan", "18F"])
def test_live_junk_names_are_not_aliases(name):
    assert is_usable_alias(name) is False


@pytest.mark.parametrize("name,expected", [
    ("18F-Florbetapir or 18F-Flutemetamol", True),
    ("Conventional medical treatment：donepezil, rivastigmine or galantamine", True),
    ("Donepezil", False),
    ("Donepezil + Memantine", False),
    ("Orexin antagonist", False),          # 'or' inside a word is not a separator
])
def test_lists_alternatives(name, expected):
    assert lists_alternatives(name) is expected


def test_alternatives_are_never_aliases():
    assert is_usable_alias("18F-Florbetapir or 18F-Flutemetamol") is False


@pytest.mark.parametrize("candidates,display", [
    # Live strings: a multi-word description never replaces a code.
    (["BMS-708163", "gamma secretase inhibitor"], "BMS-708163"),
    (["AADvac1", "coupled to KLH"], "AADvac1"),
    (["XPro1595", "Dominant-negative Tumor Necrosis Factor", "DN-TNF"], "XPro1595"),
    (["[18F]MNI-815", "FBB"], "[18F]MNI-815"),
    # A one-word nonproprietary name still does.
    (["ABBV-8E12", "Tilavonemab"], "tilavonemab"),
    (["MK-8931", "verubecestat"], "verubecestat"),
])
def test_choose_display_never_prefers_description_over_code(candidates, display):
    assert choose_display_name(candidates) == display
