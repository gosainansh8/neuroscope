"""Synthetic CT.gov v2 study records for tests.

NCT IDs in the 9xxxxxxx range are fake on purpose. These fixtures test
parsing and normalization logic; they are not claims about real trials.
"""

from __future__ import annotations

import copy


def study(nct_id: str, *, title: str = "Test study", status: str = "RECRUITING",
          phases: list[str] | None = None, study_type: str = "INTERVENTIONAL",
          sponsor: str = "Test Pharma Inc.", sponsor_class: str = "INDUSTRY",
          interventions: list[dict] | None = None, enrollment: int | None = 100,
          start: str | None = "2024-03", primary_completion: str | None = "2027-06",
          has_results: bool = False, version_holder: str = "2026-10-20") -> dict:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": title},
            "statusModule": {
                "overallStatus": status,
                "startDateStruct": {"date": start} if start else None,
                "primaryCompletionDateStruct": {"date": primary_completion} if primary_completion else None,
                "completionDateStruct": {"date": "2028-01-15"},
                "lastUpdatePostDateStruct": {"date": "2026-09-30"},
            },
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": sponsor, "class": sponsor_class},
                "collaborators": [{"name": "Test University", "class": "OTHER"}],
            },
            "conditionsModule": {"conditions": ["Alzheimer Disease"], "keywords": ["amyloid"]},
            "designModule": {
                "studyType": study_type,
                "phases": phases if phases is not None else ["PHASE3"],
                "designInfo": {"allocation": "RANDOMIZED", "maskingInfo": {"masking": "DOUBLE"}},
                "enrollmentInfo": {"count": enrollment, "type": "ESTIMATED"} if enrollment is not None else None,
            },
            "armsInterventionsModule": {"interventions": interventions or []},
            "outcomesModule": {
                "primaryOutcomes": [{"measure": "Change from baseline in CDR-SB", "timeFrame": "Week 76"}],
                "secondaryOutcomes": [{"measure": "Change in amyloid PET", "timeFrame": "Week 76"}],
            },
        },
        "derivedSection": {"miscInfoModule": {"versionHolder": version_holder}},
        "hasResults": has_results,
    }


def iv(name: str, type_: str = "DRUG", other_names: list[str] | None = None) -> dict:
    d = {"type": type_, "name": name, "armGroupLabels": ["Arm A"]}
    if other_names:
        d["otherNames"] = other_names
    return d


def all_studies() -> list[dict]:
    return [
        # Seeded drug via dose-laden name plus a code otherName; placebo arm.
        study("NCT90000001", phases=["PHASE3"],
              interventions=[iv("Lecanemab 10 mg/kg IV biweekly", "BIOLOGICAL", ["BAN2401"]), iv("Placebo")]),
        # Seeded drug with alias in parentheses; combined phase; results posted.
        study("NCT90000002", phases=["PHASE2", "PHASE3"], status="COMPLETED", has_results=True,
              interventions=[iv("Donanemab (LY3002813)", "BIOLOGICAL"), iv("Matching placebo")]),
        # Unseeded drug: code name registered, INN-like name in otherNames.
        study("NCT90000003", phases=["PHASE1"], sponsor="Startup Bio",
              interventions=[iv("XYZ-1234", other_names=["fakeumab"])]),
        # Same unseeded drug, different spelling of the code. Must resolve to the same drug.
        study("NCT90000004", phases=["PHASE2"], sponsor="Startup Bio",
              interventions=[iv("XYZ1234 oral tablet")]),
        # Conflict: names point at two different seeded drugs. Must not merge them.
        study("NCT90000005", phases=["PHASE2"],
              interventions=[iv("Lecanemab", "BIOLOGICAL", ["Aducanumab"])]),
        # Non-drug intervention only.
        study("NCT90000006", phases=["NA"], sponsor="Test University", sponsor_class="OTHER",
              interventions=[iv("Computerized cognitive training", "BEHAVIORAL")]),
        # Salt form of a seeded drug.
        study("NCT90000008", phases=["PHASE4"], status="TERMINATED",
              interventions=[iv("Donepezil Hydrochloride 10 mg")]),
        # Malformed: no NCT ID. Must fail alone without stopping the run.
        {"protocolSection": {"identificationModule": {"briefTitle": "Broken record"}}},
    ]


def mutated(raw: dict, **status_changes) -> dict:
    out = copy.deepcopy(raw)
    sm = out["protocolSection"]["statusModule"]
    for k, v in status_changes.items():
        sm[k] = v
    return out
