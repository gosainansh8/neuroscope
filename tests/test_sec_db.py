"""SEC EDGAR disclosures: loading with a stand-in for EDGAR (no network), strict drug matching, citations."""

import json
from datetime import date

import pytest

from neuroscope import sec, tools
from neuroscope.agent import ToolCall, check_citations, source_links
from neuroscope.ingest import run_ingestion
from tests.fixtures import iv, study

TODAY = date.today().isoformat()
RELEASE = """<html><body><p>EXHIBIT 99.1</p><p>Fakeumab Phase 3 Trial Meets Primary Endpoint in Early Alzheimer's</p>
<p>FAKE PHARMA today announced that fakeumab slowed decline on CDR-SB. The same results were seen in the
SAM-e arm (not a drug here). Insulin sensitivity was unchanged.</p></body></html>"""
UNRELATED = "<html><body><p>Fake Pharma Announces Chief Financial Officer Transition</p><p>Nothing else.</p></body></html>"


class FakeEdgar:
    def __init__(self):
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        if url == sec.TICKERS_URL:
            data = {"0": {"cik_str": 999999, "ticker": "FAKE", "title": "FAKE PHARMA INC"}}
        elif "submissions" in url:
            data = {"filings": {"recent": {
                "accessionNumber": ["0000999999-26-000001", "0000999999-26-000002", "0000999999-26-000003"],
                "filingDate": [TODAY, TODAY, TODAY], "form": ["8-K", "8-K", "8-K"],
                "items": ["8.01,9.01", "5.02", "7.01"], "primaryDocument": ["a.htm", "b.htm", "c.htm"]}}}
        elif url.endswith("index.json"):
            data = {"directory": {"item": [{"name": "c.htm"}, {"name": "ex99-1.htm"}]}}
        elif url.endswith("ex99-1.htm") and "000099999926000001" in url:
            return type("R", (), {"text": RELEASE, "raise_for_status": lambda self: None})()
        else:
            return type("R", (), {"text": UNRELATED, "raise_for_status": lambda self: None})()
        return type("R", (), {"json": lambda self: data, "raise_for_status": lambda self: None,
                              "text": json.dumps(data)})()


@pytest.fixture()
def loaded(conn, monkeypatch, tmp_path):
    run_ingestion(conn, [study("NCT99100001", interventions=[iv("fakeumab")]),
                         study("NCT99100002", phases=["PHASE2"], interventions=[iv("SAM-e")]),
                         study("NCT99100003", interventions=[iv("Insulin")])], "test", "synthetic", disease="alzheimers")
    companies = tmp_path / "companies.csv"
    companies.write_text("ticker,company,why\nFAKE,Fake Pharma,test\nGONE,Gone Inc,delisted\n", encoding="utf-8")
    monkeypatch.setattr(sec, "COMPANIES", companies)
    monkeypatch.setattr(sec, "PAUSE_S", 0)
    edgar = FakeEdgar()
    summary = sec.load(conn, session=edgar)
    return summary, edgar


def test_load_keeps_only_filings_that_name_a_tracked_drug(conn, loaded):
    summary, edgar = loaded
    assert summary["unknown_tickers"] == ["GONE"]
    # Item 5.02 (an officer change) is not a press-release filing and is not read at all.
    assert not any("000099999926000002" in u for u in edgar.urls)
    assert (summary["new"], summary["no_drug"]) == (1, 1)
    row = conn.execute("SELECT source_type, external_id, title, journal FROM documents").fetchone()
    assert row == ("press_release", "0000999999-26-000001",
                   "Fakeumab Phase 3 Trial Meets Primary Endpoint in Early Alzheimer's", "Fake Pharma (SEC Form 8-K)")
    # Strict matching: fakeumab links; "same" does not match SAM-e, and insulin is a generic substance.
    linked = [r[0] for r in conn.execute("SELECT d.canonical_name FROM document_drugs dd JOIN drugs d ON d.id = dd.drug_id")]
    assert linked == ["fakeumab"]
    # Loading again stores nothing twice.
    assert sec.load(conn, session=FakeEdgar())["unchanged"] == 1


def test_disclosures_are_searchable_and_cited_by_accession(conn, loaded):
    result = tools.search_disclosures(conn, "primary endpoint CDR-SB", drug="fakeumab")
    hit = result["results"][0]
    assert hit["accession"] == "0000999999-26-000001" and hit["linked_drugs"] == ["fakeumab"]
    assert tools.search_literature(conn, "primary endpoint CDR-SB")["results"] == []     # not mixed into papers
    steps = [(ToolCall("search_disclosures", {"query": "x"}), result)]
    cited, bad = check_citations("Fake Pharma said fakeumab met its endpoint (0000999999-26-000001); "
                                 "see also 0000111111-26-000009.", steps)
    assert cited["sec_filing"] == ["0000999999-26-000001"] and bad["sec_filing"] == ["0000111111-26-000009"]
    assert source_links(steps) == {"0000999999-26-000001": hit["url"]}


def test_a_missing_contact_is_refused(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError):
        sec.user_agent()
