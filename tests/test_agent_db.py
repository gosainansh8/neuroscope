"""Agent tools, loop, and citation check. Real Postgres, synthetic data, a scripted stand-in model."""

from neuroscope.agent import (AgentResult, ModelTurn, ToolCall, answer_question, build_tools, check_citations,
                              format_result, identifiers)
from neuroscope.fda import load, load_labels
from neuroscope.ingest import run_ingestion
from neuroscope.pubmed import parse_articles, store
from neuroscope import tools
from tests.fixtures import iv, study
from tests.test_documents_db import article, article_set
from tests.test_fda_db import app, export, label


class ScriptedModel:
    """Replies with a fixed sequence of turns and records what it was sent."""

    def __init__(self, turns):
        self.turns, self.received, self.system, self.specs = list(turns), [], None, None

    def start(self, system, tool_specs, question):
        self.system, self.specs, self.question = system, tool_specs, question
        return self.turns.pop(0)

    def send_tool_results(self, results):
        self.received.append(results)
        return self.turns.pop(0)


def seed(conn, tmp_path):
    run_ingestion(conn, [
        study("NCT95000001", title="Lecanemab confirmatory study", phases=["PHASE3"], status="RECRUITING",
              interventions=[iv("Lecanemab", "BIOLOGICAL"), iv("Placebo")]),
        study("NCT95000002", title="Masitinib add-on study", phases=["PHASE3"], status="COMPLETED",
              has_results=True, interventions=[iv("Masitinib")]),
        study("NCT95000003", title="Semaglutide in early AD", phases=["PHASE3"], status="RECRUITING",
              interventions=[iv("Semaglutide")]),
    ], "test", "synthetic")
    load(conn, export(tmp_path, [app("BLA999001", ["LECANEMAB-IRMB"], brand="LEQEMBI"),
                                 app("NDA999040", ["SEMAGLUTIDE"], brand="OZEMPIC", orig=("AP", "20171205"))]))
    load_labels(conn, iter([
        label("set-leq", ["BLA999001"], "LEQEMBI is indicated for the treatment of Alzheimer's disease."),
        label("set-oze", ["NDA999040"], "OZEMPIC is indicated for type 2 diabetes mellitus.")]))
    store(conn, parse_articles(article_set(article("900000021", "Lecanemab and ARIA.", [
        (None, "Lecanemab was associated with amyloid-related imaging abnormalities.")]))))


# --- Tools -------------------------------------------------------------------

def test_find_drug_resolves_trusted_names_and_never_guesses(conn, tmp_path):
    seed(conn, tmp_path)
    found = tools.find_drug(conn, "Leqembi")
    assert found["canonical_name"] == "lecanemab" and "BAN2401" in found["verified_names"]
    missing = tools.find_drug(conn, "lecan")
    assert missing["found"] is False and missing["similar_names"] == ["lecanemab"]


def test_get_trials_lists_trials_with_the_phase_caveat(conn, tmp_path):
    seed(conn, tmp_path)
    r = tools.get_trials(conn, "BAN2401")
    assert r["summary"]["n_trials"] == 1 and r["summary"]["highest_phase_active"] == 3.0
    assert r["trials"][0]["nct_id"] == "NCT95000001" and r["trials"][0]["url"].endswith("/NCT95000001")
    assert "not regulatory status" in r["caveat"]
    assert tools.get_trials(conn, "notadrug")["found"] is False


def test_get_fda_status_separates_three_cases(conn, tmp_path):
    seed(conn, tmp_path)
    lec = tools.get_fda_status(conn, "lecanemab")
    assert (lec["first_approval_date"], lec["label_indications_mention"]["alzheimers"]) == ("2023-01-06", True)
    assert lec["applications"][0]["application_number"] == "BLA999001"
    assert "treatment of Alzheimer's disease" in lec["label"]["indications_and_usage_text"]
    sem = tools.get_fda_status(conn, "semaglutide")                 # approved, but not for Alzheimer's
    assert sem["fda_approval_record"] is True and sem["label_indications_mention"]["alzheimers"] is False
    mas = tools.get_fda_status(conn, "masitinib")                   # completed Phase 3, no FDA record
    assert mas["fda_approval_record"] is False and "not proof" in mas["caveat"]


def test_search_literature_and_list_pipeline(conn, tmp_path):
    seed(conn, tmp_path)
    lit = tools.search_literature(conn, "imaging abnormalities", drug="lecanemab")
    assert lit["mode"] == "keyword" and [r["pmid"] for r in lit["results"]] == ["900000021"]
    pipe = tools.list_pipeline(conn, min_phase=3, active_only=False)
    by_drug = {d["drug"]: d for d in pipe["drugs"]}
    assert by_drug["masitinib"]["fda_first_approval_date"] is None
    assert by_drug["lecanemab"]["fda_label_mentions_alzheimers"] is True
    assert set(by_drug) == {"lecanemab", "masitinib", "semaglutide"}


# --- Citation check ------------------------------------------------------------

def test_identifiers_are_extracted_in_their_written_forms():
    ids = identifiers("See NCT95000001, EMEA/H/C/005966 and BLA999001 (PMID 900000021; https://pubmed.ncbi.nlm.nih.gov/900000022/).")
    assert ids == {"nct": {"NCT95000001"}, "fda_application": {"BLA999001"}, "pmid": {"900000021", "900000022"},
                   "ema_product": {"EMEA/H/C/005966"}, "sec_filing": set()}


def test_fda_application_numbers_are_checked_however_they_are_spaced():
    # Seen live: the model wrote "NDA 209637" and the check, which expected "NDA209637", skipped it.
    assert identifiers("NDA 209637, BLA-761269 and ANDA090178")["fda_application"] == {
        "NDA209637", "BLA761269", "ANDA090178"}
    steps = [(ToolCall("get_fda_status", {}), {"applications": [{"application_number": "NDA209637"}]})]
    cited, bad = check_citations("Approved under NDA 209637; also NDA 123456.", steps)
    assert cited["fda_application"] == ["NDA209637"] and bad["fda_application"] == ["NDA123456"]


def test_check_citations_flags_identifiers_no_tool_returned():
    steps = [(ToolCall("get_trials", {}), {"trials": [{"nct_id": "NCT95000001"}]}),
             (ToolCall("search_literature", {}), {"results": [{"pmid": "900000021"}]})]
    cited, bad = check_citations("NCT95000001 and NCT95009999 are relevant (PMID 900000021, PMID 12345678).", steps)
    assert cited == {"nct": ["NCT95000001"], "fda_application": [], "pmid": ["900000021"], "ema_product": [], "sec_filing": []}
    assert bad == {"nct": ["NCT95009999"], "fda_application": [], "pmid": ["12345678"], "ema_product": [], "sec_filing": []}


# --- The loop ------------------------------------------------------------------

def test_agent_runs_tools_and_returns_a_checked_answer(conn, tmp_path):
    seed(conn, tmp_path)
    model = ScriptedModel([
        ModelTurn([ToolCall("find_drug", {"name": "Leqembi"})]),
        ModelTurn([ToolCall("get_fda_status", {"drug": "lecanemab"}), ToolCall("get_trials", {"drug": "lecanemab"})]),
        ModelTurn(text="Lecanemab is FDA-approved (BLA999001, first approved 2023-01-06). "
                       "It has one active Phase 3 trial (NCT95000001)."),
    ])
    result = answer_question("What is the status of Leqembi?", model, build_tools(conn))
    assert [c.name for c, _ in result.steps] == ["find_drug", "get_fda_status", "get_trials"]
    assert result.cited == {"nct": ["NCT95000001"], "fda_application": ["BLA999001"], "pmid": [], "ema_product": [], "sec_filing": []}
    assert not any(result.unsupported.values()) and result.stopped_early is False
    # The model was sent the real tool output, the rules, and all five tools.
    assert model.received[1][0][1]["first_approval_date"] == "2023-01-06"
    assert "Never infer approval" in model.system and len(model.specs) == 9
    assert "all 2 identifiers cited above came from database lookups" in format_result(result)


def test_agent_flags_a_made_up_citation(conn, tmp_path):
    seed(conn, tmp_path)
    model = ScriptedModel([ModelTurn([ToolCall("get_trials", {"drug": "masitinib"})]),
                           ModelTurn(text="Masitinib was approved under NDA123456 after NCT95000002.")])
    result = answer_question("Is masitinib approved?", model, build_tools(conn))
    assert result.unsupported["fda_application"] == ["NDA123456"] and result.cited["nct"] == ["NCT95000002"]
    assert "WARNING: 1 identifier(s)" in format_result(result) and "NDA123456" in format_result(result)


def test_agent_survives_bad_tool_calls_and_stops_at_the_step_limit(conn, tmp_path):
    seed(conn, tmp_path)
    model = ScriptedModel([ModelTurn([ToolCall("delete_everything", {}), ToolCall("get_trials", {"wrong": 1})]),
                           ModelTurn(text="I could not look that up.")])
    result = answer_question("?", model, build_tools(conn))
    assert [r["error"].split(":")[0] for _, r in result.steps] == ["unknown tool 'delete_everything'",
                                                                    "get_trials could not run"]
    # Out of lookups, the model gets one more turn to answer from what it has (the lookup is not run).
    wrapping_up = ScriptedModel([ModelTurn([ToolCall("list_pipeline", {})])] * 4
                                + [ModelTurn(text="From the pipeline lookups so far: lecanemab.")])
    late = answer_question("?", wrapping_up, build_tools(conn), max_steps=3)
    assert late.stopped_early is True and len(late.steps) == 3 and late.answer.startswith("From the pipeline")
    assert "Lookup limit reached" in wrapping_up.received[-1][0][1]["error"]
    # A model that still asks for lookups after that gets the plain step-limit message.
    looping = ScriptedModel([ModelTurn([ToolCall("list_pipeline", {})])] * 5)
    stuck = answer_question("?", looping, build_tools(conn), max_steps=3)
    assert stuck.stopped_early is True and len(stuck.steps) == 3 and "step limit" in stuck.answer
    assert isinstance(stuck, AgentResult)


# --- Attribution check -----------------------------------------------------------

from neuroscope.agent import check_attribution  # noqa: E402


def test_attribution_flags_a_citation_attached_to_the_wrong_drug():
    steps = [
        (ToolCall("get_trials", {"drug": "lecanemab"}), {"canonical_name": "lecanemab", "verified_names": ["Leqembi", "BAN2401"],
                                                       "trials": [{"nct_id": "NCT95000001"}]}),
        (ToolCall("get_fda_status", {"drug": "semaglutide"}), {"canonical_name": "semaglutide", "brand_names": ["OZEMPIC"],
                                                              "applications": [{"application_number": "NDA999040"}]}),
        (ToolCall("search_literature", {}), {"results": [{"pmid": "900000021", "linked_drugs": ["lecanemab"]}]}),
    ]
    ok = "Leqembi is in NCT95000001. Ozempic was approved under NDA 999040. Lecanemab causes ARIA (PMID 900000021)."
    assert check_attribution(ok, steps) == []
    wrong = "Semaglutide was tested in NCT95000001. Lecanemab was approved under NDA999040."
    flagged = check_attribution(wrong, steps)
    assert [(w["identifier"], w["record_belongs_to"], w["sentence_names"]) for w in flagged] == [
        ("NCT95000001", ["lecanemab"], ["semaglutide"]), ("NDA999040", ["semaglutide"], ["lecanemab"])]
    # A sentence that names no drug is not judged.
    assert check_attribution("This was studied in NCT95000001.", steps) == []


# --- Follow-up questions -------------------------------------------------------------

def test_follow_up_passes_history_and_still_checks_citations_this_turn(conn, tmp_path):
    seed(conn, tmp_path)
    received = {}

    class Remembering(ScriptedModel):
        def start(self, system, tool_specs, question, history=None):
            received["history"] = history
            return super().start(system, tool_specs, question)

    history = [{"question": "Is lecanemab approved?", "answer": "Yes, under BLA999001.", "extra": "dropped"}] * 5
    model = Remembering([ModelTurn(text="Its trial is NCT95000001, approved under BLA999001.")])
    result = answer_question("What trials is it in?", model, build_tools(conn), history=history)
    # Only the last 3 turns are sent, with only question and answer.
    assert received["history"] == [{"question": "Is lecanemab approved?", "answer": "Yes, under BLA999001."}] * 3
    # Repeating identifiers from earlier turns without looking them up again is flagged.
    assert result.unsupported == {"nct": ["NCT95000001"], "fda_application": ["BLA999001"], "pmid": [], "ema_product": [], "sec_filing": []}
    assert "Earlier questions and answers" in model.system


def test_without_history_the_model_is_called_as_before(conn, tmp_path):
    seed(conn, tmp_path)
    model = ScriptedModel([ModelTurn(text="No lookups needed.")])      # its start() takes no history
    assert answer_question("Hello there", model, build_tools(conn)).answer == "No lookups needed."


def test_a_stalled_model_request_is_raced_by_a_duplicate(monkeypatch):
    """Vertex AI sometimes sends nothing for a long time; a duplicate request started after a short
    wait wins, and its reply is used."""
    import threading
    from neuroscope import agent

    release = threading.Event()
    calls = []

    class Models:
        def generate_content_stream(self, model, contents, config):
            calls.append(len(calls))
            if len(calls) == 1:                       # the first request stalls
                def stalled():
                    release.wait(5)
                    yield "late"
                return stalled()
            return iter(["fresh", "reply"])

    m = agent.GeminiModel.__new__(agent.GeminiModel)
    m._client = type("C", (), {"models": Models()})()
    m.model, m._config, m._history = "test", None, []
    monkeypatch.setattr(agent, "HEDGE_AFTER_S", 0.05)
    assert list(m._hedged_stream()) == ["fresh", "reply"] and len(calls) == 2
    release.set()
