"""Cross-disease comparison: outcome vocabulary, ChEMBL mechanism matching (no network), and the lookup."""

import pytest

from neuroscope import mechanisms, tools
from neuroscope.ingest import run_ingestion
from neuroscope.outcomes import classify, terms_in
from tests.fixtures import iv, study


@pytest.mark.parametrize("text,terms", [
    ("Change from baseline in CDR-SB at week 78", ["cdr_sb"]),
    ("MDS-UPDRS Part III total score", ["mds_updrs"]),
    ("Change in daily OFF time (Hauser diary)", ["off_time"]),
    ("ALSFRS-R slope and slow vital capacity", ["alsfrs_r", "vital_capacity"]),
    ("Plasma neurofilament light chain", ["nfl"]),
    ("The study aims to assess motor function", []),          # "aims" is not the AIMS scale
    ("Muscle atrophy measured by ultrasound", []),            # not brain atrophy
    ("Effect on time to first fall", []),                     # not ON time
])
def test_outcome_vocabulary(text, terms):
    assert terms_in(text) == terms


def ad_and_pd_trials(conn):
    ad = study("NCT98000001", title="Fakeumab in Alzheimer's", interventions=[iv("fakeumab")])
    pd = study("NCT98000002", title="Fakeumab in Parkinson's disease", interventions=[iv("fakeumab")])
    pd["protocolSection"]["outcomesModule"] = {
        "primaryOutcomes": [{"measure": "MDS-UPDRS Part III"}],
        "secondaryOutcomes": [{"measure": "DaT-SPECT striatal binding"}]}
    run_ingestion(conn, [ad], "test", "synthetic", disease="alzheimers")
    run_ingestion(conn, [pd], "test", "synthetic", disease="parkinsons")


def test_endpoints_and_biomarkers_compare_by_disease(conn):
    ad_and_pd_trials(conn)
    assert classify(conn)["trials_with_a_term"] == 2
    ends = tools.compare_diseases(conn, "endpoints", ["alzheimers", "parkinsons"])
    assert ends["by_disease"]["alzheimers"]["top"][0] == {
        "term": "CDR Sum of Boxes", "n_trials": 1, "share_of_trials": 1.0, "example_trials": ["NCT98000001"]}
    assert ends["by_disease"]["parkinsons"]["top"][0]["term"] == "MDS-UPDRS / UPDRS"
    bio = tools.compare_diseases(conn, "biomarkers", primary_only=False)
    assert [t["term"] for t in bio["by_disease"]["parkinsons"]["top"]] == ["Dopamine transporter imaging (DaT-SPECT)"]
    assert bio["by_disease"]["als"]["trials_considered"] == 0
    with pytest.raises(ValueError):
        tools.compare_diseases(conn, "colors")


class FakeChembl:
    """Answers the three ChEMBL calls the loader makes."""

    def __init__(self, molecules):
        self.molecules = molecules

    def get(self, url, params=None, timeout=None):
        if url.endswith("/molecule/search.json"):
            data = {"molecules": self.molecules.get(params["q"].lower(), [])}
        elif url.endswith("/mechanism.json"):
            data = {"mechanisms": [{"mechanism_of_action": "Fake receptor antagonist", "action_type": "ANTAGONIST",
                                    "target_chembl_id": "CHEMBL9"}] if params["parent_molecule_chembl_id"] == "CHEMBL1" else []}
        else:
            data = {"pref_name": "Fake receptor", "target_type": "SINGLE PROTEIN"}
        return type("R", (), {"json": lambda self: data, "raise_for_status": lambda self: None})()


def test_mechanisms_match_trusted_names_only(conn, monkeypatch):
    ad_and_pd_trials(conn)
    run_ingestion(conn, [study("NCT98000003", interventions=[iv("otherumab")]),
                         study("NCT98000004", interventions=[iv("otherumab")])], "test", "synthetic",
                  disease="alzheimers")
    monkeypatch.setattr(mechanisms, "PAUSE_S", 0)
    fake = FakeChembl({
        "fakeumab": [{"molecule_chembl_id": "CHEMBL1", "pref_name": "FAKEUMAB", "molecule_synonyms": [],
                      "molecule_hierarchy": {"parent_chembl_id": "CHEMBL1"}}],
        # A search hit whose names do not equal any trusted name of otherumab is not accepted.
        "otherumab": [{"molecule_chembl_id": "CHEMBL2", "pref_name": "OTHERUMAB PEGOL", "molecule_synonyms": [],
                       "molecule_hierarchy": {"parent_chembl_id": "CHEMBL2"}}]})
    s = mechanisms.load(conn, min_trials=1, session=fake)
    assert (s["drugs_looked_up"], s["drugs_matched"], s["drugs_with_mechanism"]) == (2, 1, 1)
    assert tools.find_drug(conn, "fakeumab")["mechanisms_chembl"] == [
        {"mechanism": "Fake receptor antagonist", "action_type": "ANTAGONIST", "target": "Fake receptor"}]
    mech = tools.compare_diseases(conn, "mechanisms")
    assert mech["by_disease"]["parkinsons"]["top"][0]["example_drugs"] == ["fakeumab"]
    assert mech["in_every_top_list"] == []                    # ALS has none
