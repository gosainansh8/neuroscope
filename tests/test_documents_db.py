"""PubMed parsing, storage, drug linking, and keyword search. Synthetic abstracts (fake PMIDs)."""

import pytest

from neuroscope.ingest import run_ingestion
from neuroscope.pubmed import chunk_text, parse_articles, search_term, store
from neuroscope.search import search
from tests.fixtures import iv, study


def article(pmid, title, sections, *, journal="Test Journal of Neurology", pubdate="<Year>2023</Year><Month>Jan</Month><Day>05</Day>"):
    def section(label, text):
        attr = ' Label="%s"' % label if label else ""
        return f"<AbstractText{attr}>{text}</AbstractText>"
    body = "".join(section(label, text) for label, text in sections)
    abstract = f"<Abstract>{body}</Abstract>" if sections else ""
    return (f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><Journal><JournalIssue>"
            f"<PubDate>{pubdate}</PubDate></JournalIssue><Title>{journal}</Title></Journal>"
            f"<ArticleTitle>{title}</ArticleTitle>{abstract}</Article></MedlineCitation></PubmedArticle>")


def article_set(*articles):
    return "<PubmedArticleSet>" + "".join(articles) + "</PubmedArticleSet>"


LECANEMAB = article("900000001", "Lecanemab in early Alzheimer's disease.", [
    ("BACKGROUND", "Lecanemab (BAN2401) binds amyloid-beta protofibrils."),
    ("RESULTS", "Amyloid-related imaging abnormalities occurred in 12.6% of participants. "
                "Cognitive decline on the CDR-SB was slower than with placebo."),
])
DONEPEZIL = article("900000002", "Donepezil and <i>memantine</i> in moderate Alzheimer's disease.", [
    (None, "Aricept was compared with Namenda. Cognitive decline was measured over 52 weeks."),
], pubdate="<MedlineDate>2019 Nov-Dec</MedlineDate>")
NO_ABSTRACT = article("900000003", "A letter with no abstract.", [])
UNLINKED = article("900000004", "Sleep and cognitive decline in older adults.", [
    (None, "Sleep duration was associated with cognitive decline. No drug was studied."),
], pubdate="<Year>2021</Year>")


def test_parse_articles():
    abstracts = list(parse_articles(article_set(LECANEMAB, DONEPEZIL, NO_ABSTRACT, UNLINKED)))
    assert [a.pmid for a in abstracts] == ["900000001", "900000002", "900000004"]     # no abstract: skipped
    lec, don, unl = abstracts
    assert lec.title == "Lecanemab in early Alzheimer's disease."
    assert lec.text.startswith("BACKGROUND: Lecanemab (BAN2401)") and "\nRESULTS: Amyloid-related" in lec.text
    assert (lec.published_date.isoformat(), lec.published_date_precision) == ("2023-01-05", "day")
    assert don.title == "Donepezil and memantine in moderate Alzheimer's disease."    # inline tags flattened
    assert (don.published_date.isoformat(), don.published_date_precision) == ("2019-11-01", "month")
    assert (unl.published_date.isoformat(), unl.published_date_precision) == ("2021-01-01", "year")


def test_chunk_text_overlaps_long_text():
    words = [f"w{i}" for i in range(500)]
    chunks = chunk_text(" ".join(words), size=220, overlap=40)
    assert [len(c.split()) for c in chunks] == [220, 220, 140]
    assert chunks[1].split()[0] == "w180" and chunks[-1].split()[-1] == "w499"
    assert chunk_text("short abstract") == ["short abstract"] and chunk_text("") == []


def test_search_term_drops_the_radiolabel():
    assert search_term("[18F]florbetapir") == "florbetapir" and search_term("BMS-708163") == "BMS-708163"


def links(conn, pmid):
    return conn.execute(
        "SELECT dr.canonical_name, dd.matched_name FROM document_drugs dd JOIN documents d ON d.id = dd.document_id "
        "JOIN drugs dr ON dr.id = dd.drug_id WHERE d.external_id = %s ORDER BY 1", (pmid,)).fetchall()


def test_store_links_documents_by_trusted_names_only(conn):
    # 'ABC-100' is an unreviewed alias of the auto-created drug 'fakeumab'.
    run_ingestion(conn, [study("NCT94000001", interventions=[iv("ABC-100", other_names=["fakeumab"])])],
                  "test", "synthetic")
    mentions = article("900000005", "ABC-100 and fakeumab pharmacology.", [(None, "ABC-100 lowered tau. Donepezilat is not a drug.")])
    s = store(conn, parse_articles(article_set(LECANEMAB, DONEPEZIL, UNLINKED, mentions)))
    assert s == {"abstracts": 4, "new": 4, "changed": 0, "unchanged": 0, "duplicates": 0, "chunks": 4, "drug_links": 4}
    assert links(conn, "900000001") == [("lecanemab", "lecanemab")]
    # Brand names are verified seed aliases, so they link too.
    assert links(conn, "900000002") == [("donepezil", "donepezil"), ("memantine", "memantine")]
    assert links(conn, "900000004") == []
    # The drug's own name links; its unreviewed code does not; 'Donepezilat' is not a whole-word match.
    assert links(conn, "900000005") == [("fakeumab", "fakeumab")]
    row = conn.execute("SELECT url, journal, published_date::text, published_date_precision FROM documents "
                       "WHERE external_id = '900000001'").fetchone()
    assert row == ("https://pubmed.ncbi.nlm.nih.gov/900000001/", "Test Journal of Neurology", "2023-01-05", "day")


def test_store_is_idempotent_and_rechunks_changed_abstracts(conn):
    store(conn, parse_articles(article_set(LECANEMAB, UNLINKED)))
    again = store(conn, parse_articles(article_set(LECANEMAB, UNLINKED)))
    assert (again["new"], again["changed"], again["unchanged"], again["chunks"]) == (0, 0, 2, 0)
    revised = article("900000001", "Lecanemab in early Alzheimer's disease.", [(None, "Corrected abstract about lecanemab.")])
    changed = store(conn, parse_articles(article_set(revised)))
    assert (changed["new"], changed["changed"], changed["chunks"]) == (0, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    assert conn.execute("SELECT c.text FROM chunks c JOIN documents d ON d.id = c.document_id "
                        "WHERE d.external_id = '900000001'").fetchall() == [("Corrected abstract about lecanemab.",)]


def test_keyword_search_ranks_and_filters_by_drug(conn):
    store(conn, parse_articles(article_set(LECANEMAB, DONEPEZIL, UNLINKED)))
    hits = search(conn, "cognitive decline")
    assert {h.url for h in hits} == {f"https://pubmed.ncbi.nlm.nih.gov/{p}/" for p in ("900000001", "900000002", "900000004")}
    assert all("**" in h.snippet for h in hits)
    # Stemming: 'abnormality' finds 'abnormalities'.
    assert [h.title for h in search(conn, "amyloid imaging abnormality")] == ["Lecanemab in early Alzheimer's disease."]
    # A drug filter accepts any trusted name, here a seeded code.
    only = search(conn, "cognitive decline", drug="BAN2401")
    assert [(h.title, h.drugs, h.published_date) for h in only] == [
        ("Lecanemab in early Alzheimer's disease.", ["lecanemab"], "2023-01-05")]
    assert search(conn, "zebrafish") == []
    with pytest.raises(ValueError):
        search(conn, "cognitive decline", drug="notadrug")


def test_one_failing_drug_is_skipped_and_the_rest_still_load(conn, monkeypatch):
    # Live case: PubMed answered one efetch with "502 Bad Gateway" and the whole step failed.
    import requests
    from neuroscope import pubmed
    from neuroscope.ingest import run_ingestion
    from tests.fixtures import iv, study
    run_ingestion(conn, [study(f"NCT9700000{i}", interventions=[iv(d, "BIOLOGICAL")])
                         for i, d in enumerate(["Lecanemab"] * 5 + ["Donepezil"] * 5)], "test", "synthetic")
    monkeypatch.setattr(pubmed, "PAUSE_S", 0)
    monkeypatch.setattr(pubmed, "fetch_pmids", lambda session, term, n: {"lecanemab": ["900000001"],
                                                                          "donepezil": ["900000002"]}[term])

    def fetch_xml(session, pmids):
        if pmids == ["900000001"]:
            raise requests.HTTPError("502 Server Error: Bad Gateway")
        return article_set(DONEPEZIL)
    monkeypatch.setattr(pubmed, "fetch_xml", fetch_xml)
    skipped = []
    got = list(pubmed.iter_live(conn, min_trials=5, per_drug=20, max_drugs=None, skipped=skipped))
    assert [a.pmid for a in got] == ["900000002"] and skipped == ["lecanemab"]


def test_pubmed_session_retries_its_read_only_post():
    from neuroscope.ctgov import _session
    adapter = _session(("GET", "POST")).get_adapter("https://eutils.ncbi.nlm.nih.gov")
    assert "POST" in adapter.max_retries.allowed_methods and 502 in adapter.max_retries.status_forcelist
    assert "POST" not in _session().get_adapter("https://clinicaltrials.gov").max_retries.allowed_methods


def test_relink_follows_corrections_and_skips_drug_classes(conn):
    from neuroscope.curate import map_alias
    from neuroscope.ingest import run_ingestion
    from neuroscope.pubmed import relink
    from tests.fixtures import iv, study
    run_ingestion(conn, [study("NCT97000101", interventions=[iv("Cholinesterase inhibitors")])], "test", "synthetic")
    paper = article("900000031", "Cholinesterase inhibitors and Zunveyl.", [(None, "Zunveyl was compared with cholinesterase inhibitors.")])
    store(conn, parse_articles(article_set(paper)))
    assert links(conn, "900000031") == [("cholinesterase inhibitors", "Cholinesterase inhibitors")]
    conn.execute("UPDATE drugs SET is_class = TRUE WHERE canonical_name = 'cholinesterase inhibitors'")
    map_alias(conn, "Zunveyl", "benzgalantamine", note="brand of benzgalantamine")
    assert relink(conn) == {"documents": 1, "drug_links": 1}
    assert links(conn, "900000031") == [("benzgalantamine", "Zunveyl")]


def test_identical_abstract_under_a_second_pmid_is_skipped_not_fatal(conn):
    # Seen live: PubMed returned the same title and abstract under two PMIDs, and the unique
    # content hash made the whole load fail.
    twin = LECANEMAB.replace("<PMID>900000001</PMID>", "<PMID>900000009</PMID>")
    s = store(conn, parse_articles(article_set(LECANEMAB, twin, DONEPEZIL)))
    assert (s["new"], s["duplicates"]) == (2, 1)
    assert [r[0] for r in conn.execute("SELECT external_id FROM documents ORDER BY 1")] == ["900000001", "900000002"]
