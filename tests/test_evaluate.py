"""Scoring rules of the evaluation harness. No database or model needed."""

import json

import pytest

from neuroscope.agent import AgentResult, ToolCall
from neuroscope.db import ROOT
from neuroscope.evaluate import score, verdict


@pytest.mark.parametrize("answer,expected", [
    ("**Yes**, lecanemab is FDA-approved for Alzheimer's disease.", "yes"),
    ("No. Semaglutide is not approved for Alzheimer's disease.", "no"),
    ("Semaglutide is FDA-approved for other uses, but it is not approved for Alzheimer's disease.", "no"),
    ("There is **no matched FDA approval record** for masitinib.", "no"),
    ("Lecanemab is FDA-approved for the treatment of Alzheimer's disease.", "yes"),
    ("### Regulatory Status\nDonanemab was approved on 2024-07-02.", None),     # heading is the opening line
    ("Here is what the database says.", None),
])
def test_verdict(answer, expected):
    assert verdict(answer) == expected


def result(answer, cited=None, unsupported=None, tools=()):
    empty = {"nct": [], "fda_application": [], "pmid": []}
    return AgentResult(answer, [(ToolCall(t, {}), {}) for t in tools], {**empty, **(cited or {})},
                       {**empty, **(unsupported or {})})


def test_score_passes_a_correct_answer_and_names_each_failure():
    case = {"verdict": "yes", "must_mention": ["2023-01-06"], "must_cite_any": ["BLA761269", "BLA761375"],
            "must_call": ["get_fda_status"]}
    good = score(case, result("Yes, approved on 2023-01-06 (BLA761269).", {"fda_application": ["BLA761269"]},
                              tools=["find_drug", "get_fda_status"]))
    assert all(ok for ok, _ in good.values())
    bad = score(case, result("Yes, approved under NDA123456.", unsupported={"fda_application": ["NDA123456"]}))
    failed = {k for k, (ok, _) in bad.items() if not ok}
    assert failed == {"mentions /2023-01-06/", "cites an expected record", "called get_fda_status", "no unsupported citations"}


def test_expect_missing_and_must_not_mention():
    case = {"expect_missing": True, "must_not_mention": ["the best (drug|treatment) is"]}
    assert all(ok for ok, _ in score(case, result("The database does not track revenue.")).values())
    failed = score(case, result("The best drug is lecanemab."))
    assert not failed["says the data does not cover it"][0] and not failed["avoids /the best (drug|treatment) is/"][0]


def test_cases_file_is_well_formed():
    cases = json.loads((ROOT / "evals" / "cases.json").read_text())["cases"]
    assert len(cases) >= 35 and len({c["id"] for c in cases}) == len(cases)
    allowed = {"id", "category", "question", "verdict", "must_mention", "must_not_mention", "must_cite_any",
               "min_pmids", "must_call", "expect_missing"}
    for c in cases:
        assert set(c) <= allowed, c["id"]
        assert c.get("verdict") in (None, "yes", "no")
