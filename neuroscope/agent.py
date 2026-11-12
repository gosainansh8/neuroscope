"""A question-answering agent over the NeuroScope database.

Usage:
    python -m neuroscope.agent "What is the regulatory status of lecanemab?"
    python -m neuroscope.agent "Which drugs have active Phase 3 trials but no FDA record?" --show-steps
    python -m neuroscope.agent "opicapone in Parkinson's disease" --report

HOW IT WORKS
The model cannot see the database. It can only call the read-only tools in
tools.py, read what they return, and write an answer. The loop is:
  1. Send the question and the tool descriptions to the model.
  2. If the model asks for a tool, run it and send the result back. Repeat.
  3. When the model answers in text, check the answer and return it.

WHAT KEEPS IT HONEST
  - Instructions: answer only from tool results, cite identifiers, say so when
    the data does not answer the question, never infer approval from trials.
  - A check in code, not in the prompt: every NCT ID, FDA application number,
    EMA product number, and PMID in the answer must appear in a tool result from this run. Any that
    do not are reported as unsupported. The check cannot tell whether a correctly
    cited source actually supports the sentence; that still needs a reader.

The model is Gemini on Vertex AI (NEUROSCOPE_AGENT_MODEL, default below; served
from NEUROSCOPE_AGENT_LOCATION, default "global"). Tests
use a scripted stand-in model, so they need no network.
"""

from __future__ import annotations

import argparse
import queue
import threading
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import psycopg

from . import tools
from .db import connect
from .embed import call_with_retry

DEFAULT_MODEL = "gemini-3.8-flash"
MAX_STEPS = 8
REPORT_STEPS = 14
# Measured on 2026-11-12: Vertex AI sometimes holds a request for ~40 s before answering, regardless of
# its size. Responses are always streamed, so this timeout is the longest silence allowed (not the total
# length of an answer); a stalled request is dropped and retried after 20 s instead of 45.
REQUEST_TIMEOUT_MS = 20_000
# Stalls are random (the same request answers in 2 s on a retry). If no reply has started after this
# many seconds, a duplicate request is sent and whichever starts first is used.
HEDGE_AFTER_S = 8.0
# Hidden reasoning was most of the time of the final write-up (1,688 thinking tokens for a 644-token
# answer). Capped; NEUROSCOPE_THINKING_BUDGET overrides it (-1 = the model's own default).
THINKING_BUDGET = 1024

SYSTEM_PROMPT = """You answer questions about drug development for Alzheimer's disease, Parkinson's disease, and ALS (amyotrophic lateral sclerosis) using the NeuroScope database.

Rules:
1. Use only facts returned by the tools in this conversation. Do not use your own memory for any fact about a drug, trial, approval, or paper. If the tools do not answer the question, say what is missing.
2. Cite the source of every factual claim with its identifier exactly as the tool returned it: trial IDs (NCT...), FDA application numbers (NDA..., BLA..., ANDA...), EMA product numbers (EMEA/H/C/...), SEC accession numbers (0001234567-26-000123) for company disclosures, and PubMed IDs written as "PMID 12345678". Never write an identifier that a tool did not return.
3. Approval status comes only from get_fda_status (United States) and get_ema_status (European Union). Never infer approval, or lack of it, from trial phase, trial status, or completion. If there is no FDA record, say "no matched FDA approval record" and that this is not proof the drug is unapproved. If there is no EMA record, say "no centrally authorised EU medicine", not that it is unavailable in Europe. Name the regulator for every approval you mention; when a question asks whether a drug is approved without naming a country, check both.
4. "FDA-approved" and "EU-authorised" mean approved for at least one use. Say a drug is approved for a disease only if the label or indication text returned by the tool says so, and quote the relevant words. Pass the disease to get_fda_status and get_ema_status so the right label is shown.
5. Give dates as the tools give them, and say which source date applies when it matters.
6. Mention a caveat from the tool results only when it changes how the answer should be read, in one short sentence. Do not repeat caveats the reader does not need.
7. Keep answers short and easy to scan. Start with a one- or two-sentence direct answer (for a yes-or-no question, the first word is "Yes" or "No"). Then give at most five short bullets of the most important supporting facts, each with its citation. Stay under about 150 words unless the question asks for a list or for detail. No headings, no restating the question, no sign-off. When there are many records, show the most relevant and say how many more there are. list_pipeline already reports US and EU approval for every drug it lists; do not look each drug up again unless the question needs more.
8. Trials belong to the diseases whose registry searches returned them. When a question is about one disease, pass it as `disease` to get_trials and list_pipeline; without it, counts cover every disease loaded.
9. Earlier questions and answers in this conversation are context for understanding a follow-up (for example, what "it" refers to). They are not evidence: look up again, in this turn, any fact you cite."""

REPORT_PROMPT = SYSTEM_PROMPT + """

You are writing a REPORT, not a short answer: rule 7's length limit does not apply, but keep each section tight. The user names a topic: a drug, a disease's pipeline, a
mechanism, or a comparison. Gather evidence with several lookups first (identity, trials, program status,
US and EU approval, literature, and comparison or pipeline context when relevant), then write:

## Summary
Three to five sentences with the main conclusions.
## Regulatory status
US (FDA) and EU (EMA) status, with application or product numbers and quoted indication text.
## Clinical development
Trials by phase and status, which programs are active or discontinued (quote why_stopped), key endpoints.
## Published evidence
What the abstracts report, each point cited by PMID.
## Landscape
How this compares with other drugs or diseases in the data (mechanisms, endpoints, biomarkers, pipeline).
## Gaps and caveats
What the data does not show, and the caveats returned with the lookups.

Every factual sentence carries its identifier. Leave out a section only if no lookup supports it, and say so.
Do not start with Yes or No."""

DISEASE_PARAM = {"type": "string", "enum": ["alzheimers", "parkinsons", "als"],
                 "description": "limit to one disease; omit for all"}

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "find_drug",
     "description": "Resolve a drug name (generic, brand, or code) to its canonical drug, verified names, and "
                    "trial counts by disease. Call this first when a question names a drug.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_trials",
     "description": "Clinical trial counts and the most relevant trials for one drug, from ClinicalTrials.gov, "
                    "in one disease or all.",
     "parameters": {"type": "object", "properties": {
         "drug": {"type": "string"}, "disease": DISEASE_PARAM,
         "limit": {"type": "integer", "description": "trials to list, default 8"}},
         "required": ["drug"]}},
    {"name": "get_fda_status",
     "description": "US FDA approval records and label indication text for one drug. The only source of "
                    "US approval status.",
     "parameters": {"type": "object", "properties": {
         "drug": {"type": "string"},
         "disease": {**DISEASE_PARAM, "description": "show the label that mentions this disease, if any"}},
         "required": ["drug"]}},
    {"name": "get_ema_status",
     "description": "European Medicines Agency records for one drug: centrally authorised, refused, or "
                    "withdrawn EU medicines with their indication text. The only source of EU approval status.",
     "parameters": {"type": "object", "properties": {
         "drug": {"type": "string"},
         "disease": {**DISEASE_PARAM, "description": "list medicines whose indication mentions this disease first"}},
         "required": ["drug"]}},
    {"name": "search_literature",
     "description": "Search stored PubMed abstracts by meaning and keywords. Optionally restrict to one drug. "
                    "Put the disease in the query when it matters.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "drug": {"type": "string"},
         "limit": {"type": "integer", "description": "results, default 5"}}, "required": ["query"]}},
    {"name": "get_program_status",
     "description": "Whether a drug's development program in each disease is active, discontinued, dormant, or "
                    "completed, judged from its trials, with the latest stopped trial and its stated reason. "
                    "Without a drug, lists programs in a disease with a given status (for example recently "
                    "discontinued Phase 3 programs).",
     "parameters": {"type": "object", "properties": {
         "drug": {"type": "string"}, "disease": DISEASE_PARAM,
         "status": {"type": "string", "enum": ["active", "discontinued", "dormant", "completed"]},
         "min_phase": {"type": "number", "description": "when listing: lowest highest-phase, default 0"},
         "limit": {"type": "integer"}}}},
    {"name": "compare_diseases",
     "description": "Compare Alzheimer's, Parkinson's, and ALS side by side: the most common mechanisms of action "
                    "(from ChEMBL), the standard clinical endpoints, or the biomarkers named in trial outcomes, with "
                    "counts and example drugs or trial IDs.",
     "parameters": {"type": "object", "properties": {
         "aspect": {"type": "string", "enum": ["mechanisms", "endpoints", "biomarkers"]},
         "diseases": {"type": "array", "items": {"type": "string", "enum": ["alzheimers", "parkinsons", "als"]},
                      "description": "default: all three"},
         "min_phase": {"type": "number", "description": "only trials or programs at this phase or higher, default 0"},
         "active_only": {"type": "boolean", "description": "only active trials or programs, default false"},
         "primary_only": {"type": "boolean", "description": "endpoints/biomarkers: primary outcomes only, default true"},
         "limit": {"type": "integer"}}, "required": ["aspect"]}},
    {"name": "search_disclosures",
     "description": "Search company press releases and annual reports filed with the SEC (EDGAR) for listed "
                    "companies with programs in these diseases. Use for company statements: trial readouts as "
                    "announced, launch and pipeline updates, discontinuation announcements. Cite the accession number.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "drug": {"type": "string"},
         "limit": {"type": "integer", "description": "results, default 5"}}, "required": ["query"]}},
    {"name": "list_pipeline",
     "description": "List drugs by highest trial phase, in one disease or all, with whether each has US and "
                    "EU approval records.",
     "parameters": {"type": "object", "properties": {
         "min_phase": {"type": "number", "description": "1 to 4, default 3"},
         "active_only": {"type": "boolean", "description": "count and rank by active trials only (default true); false ranks by all trials"},
         "disease": DISEASE_PARAM,
         "limit": {"type": "integer"}}}},
]

_ID_PATTERNS = {
    "nct": re.compile(r"\bNCT\d{8}\b"),
    # Models also write "NDA 209637" or "NDA-209637"; both are read as NDA209637.
    "fda_application": re.compile(r"\b(NDA|BLA|ANDA)[\s-]?(\d{6})\b"),
    "pmid": re.compile(r"(?:PMID:?\s*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{6,9})"),
    "ema_product": re.compile(r"\bEMEA/H/C/\d{6}\b"),
    "sec_filing": re.compile(r"\b\d{10}-\d{2}-\d{6}\b"),         # SEC accession number
}


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class ModelTurn:
    """One reply from the model: either tool requests or the final text."""
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str | None = None


class ChatModel(Protocol):
    def start(self, system: str, tool_specs: list[dict], question: str,
              history: list[dict] | None = None) -> ModelTurn: ...
    def send_tool_results(self, results: list[tuple[ToolCall, dict]]) -> ModelTurn: ...


@dataclass
class AgentResult:
    answer: str
    steps: list[tuple[ToolCall, dict]]
    cited: dict[str, list[str]]            # identifiers in the answer that tool results support
    unsupported: dict[str, list[str]]      # identifiers in the answer that no tool returned
    stopped_early: bool = False
    attribution_warnings: list[dict] = field(default_factory=list)   # citations next to the wrong drug


def identifiers(text: str) -> dict[str, set[str]]:
    """Identifiers in a text, in one canonical spelling each."""
    found = {kind: set(p.findall(text)) for kind, p in _ID_PATTERNS.items()}
    found["fda_application"] = {kind + number for kind, number in found["fda_application"]}
    return found


def _evidence(steps: list[tuple[ToolCall, dict]]) -> dict[str, set[str]]:
    """Every identifier that appeared in a tool result. PMIDs come from the pmid fields."""
    blob = json.dumps([result for _, result in steps], default=str)
    found = identifiers(blob)
    found["pmid"] |= set(re.findall(r'"pmid": "(\d{6,9})"', blob))
    return found


def source_links(steps: list[tuple[ToolCall, dict]]) -> dict[str, str]:
    """Links for identifiers whose public page cannot be built from the identifier alone (SEC filings)."""
    links = {}
    for call, result in steps:
        if call.name == "search_disclosures":
            for r in result.get("results") or []:
                if r.get("accession") and r.get("url"):
                    links[r["accession"]] = r["url"]
    return links


def check_citations(answer: str, steps: list[tuple[ToolCall, dict]]) -> tuple[dict, dict]:
    evidence, cited, unsupported = _evidence(steps), {}, {}
    for kind, ids in identifiers(answer).items():
        cited[kind] = sorted(ids & evidence[kind])
        unsupported[kind] = sorted(ids - evidence[kind])
    return cited, unsupported


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _owners(steps: list[tuple[ToolCall, dict]]) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Which drug each identifier came from, and a map from every drug name seen to its drug.

    A trial ID belongs to the drug whose get_trials call returned it, an FDA
    application to the drug whose get_fda_status returned it, and a PMID to the
    drugs the abstract is linked to.
    """
    owners: dict[str, set[str]] = {}
    name_to_drug: dict[str, str] = {}

    def add_name(name: str, drug: str) -> None:
        key = _norm(re.sub(r"^\[[^\]]*\]", "", name))
        if len(key) >= 4:
            name_to_drug.setdefault(key, drug)

    for call, result in steps:
        drug = result.get("canonical_name")
        if drug:
            add_name(drug, drug)
            for alias in (result.get("verified_names") or []) + (result.get("brand_names") or []):
                add_name(alias, drug)
        if call.name == "get_trials" and drug:
            for t in result.get("trials") or []:
                owners.setdefault(t["nct_id"], set()).add(drug)
        elif call.name == "get_fda_status" and drug:
            for a in result.get("applications") or []:
                owners.setdefault(a["application_number"], set()).add(drug)
            for n in ((result.get("label") or {}).get("application_numbers") or []):
                owners.setdefault(n, set()).add(drug)
        elif call.name == "get_program_status":
            for p in result.get("programs") or []:
                if p.get("latest_closed_trial"):
                    owners.setdefault(p["latest_closed_trial"], set()).add(p["drug"])
                    add_name(p["drug"], p["drug"])
        elif call.name == "get_ema_status" and drug:
            for m in result.get("medicines") or []:
                owners.setdefault(m["ema_product_number"], set()).add(drug)
                add_name(m["name"], drug)
        elif call.name == "search_disclosures":
            for r in result.get("results") or []:
                if r.get("accession"):
                    owners.setdefault(r["accession"], set()).update(r.get("linked_drugs") or [])
                for linked in r.get("linked_drugs") or []:
                    add_name(linked, linked)
        elif call.name == "search_literature":
            for r in result.get("results") or []:
                owners.setdefault(r["pmid"], set()).update(r.get("linked_drugs") or [])
                for linked in r.get("linked_drugs") or []:
                    add_name(linked, linked)
    return owners, name_to_drug


def check_attribution(answer: str, steps: list[tuple[ToolCall, dict]]) -> list[dict]:
    """Flag a citation whose sentence names drugs from this run, none of which the cited record belongs to.

    This catches "drug A ... (NCT of drug B)". It is a heuristic: a sentence that
    names no drug is not judged, and a record shared by several drugs is only
    attributed to the drugs whose lookups returned it.
    """
    owners, name_to_drug = _owners(steps)
    warnings = []
    for sentence in _SENTENCE_SPLIT.split(answer):
        flat = _norm(sentence)
        mentioned = {drug for key, drug in name_to_drug.items() if key in flat}
        if not mentioned:
            continue
        for ids in identifiers(sentence).values():
            for ident in sorted(ids):
                owned_by = owners.get(ident, set())
                if owned_by and not (mentioned & owned_by):
                    warnings.append({"identifier": ident, "sentence": sentence.strip()[:300],
                                     "record_belongs_to": sorted(owned_by), "sentence_names": sorted(mentioned)})
    return warnings


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def build_tools(conn: psycopg.Connection, embedder=None) -> dict[str, Callable[..., dict]]:
    return {
        "find_drug": lambda name: tools.find_drug(conn, name),
        "get_trials": lambda drug, limit=8, disease=None: tools.get_trials(conn, drug, min(int(limit), 15),
                                                                            disease or None),
        "get_fda_status": lambda drug, disease=None: tools.get_fda_status(conn, drug, disease or None),
        "get_ema_status": lambda drug, disease=None: tools.get_ema_status(conn, drug, disease or None),
        "search_literature": lambda query, drug=None, limit=5: tools.search_literature(
            conn, query, drug or None, min(int(limit), 8), embedder),
        "get_program_status": lambda drug=None, disease=None, status=None, min_phase=0, limit=15:
            tools.get_program_status(conn, drug or None, disease or None, status or None, float(min_phase),
                                     False, limit),
        "compare_diseases": lambda aspect, diseases=None, min_phase=0, active_only=False, primary_only=True, limit=8:
            tools.compare_diseases(conn, aspect, list(diseases) if diseases else None, float(min_phase),
                                   bool(active_only), bool(primary_only), limit),
        "search_disclosures": lambda query, drug=None, limit=5: tools.search_disclosures(
            conn, query, drug or None, min(int(limit), 8), embedder),
        "list_pipeline": lambda min_phase=3, active_only=True, limit=25, disease=None: tools.list_pipeline(
            conn, float(min_phase), bool(active_only), min(int(limit), 40), disease or None),
    }


def run_tool(registry: dict[str, Callable[..., dict]], call: ToolCall) -> dict:
    """Run one tool. Bad names or arguments come back as an error the model can read."""
    fn = registry.get(call.name)
    if fn is None:
        return {"error": f"unknown tool {call.name!r}"}
    try:
        return fn(**call.args)
    except (TypeError, ValueError) as exc:
        return {"error": f"{call.name} could not run: {exc}"}


MAX_HISTORY_TURNS = 3
STEP_LIMIT_NOTE = ("Lookup limit reached; this lookup was not run. Answer now using only the results you "
                   "already have, and say plainly what you could not check.")


def answer_question(question: str, model: ChatModel, registry: dict[str, Callable[..., dict]],
                    max_steps: int | None = None, history: list[dict] | None = None,
                    on_event: Callable[[dict], None] | None = None, mode: str = "answer") -> AgentResult:
    """Answer one question. `history` holds earlier {"question", "answer"} turns of the same
    conversation (oldest first, at most MAX_HISTORY_TURNS kept) so follow-ups can be understood.
    Citations are still checked only against lookups made in this turn.

    `on_event`, if given, is called with {"type": "lookup", "tool", "arguments"} just before each
    lookup runs, so a caller can show progress as it happens."""
    steps: list[tuple[ToolCall, dict]] = []
    history = [{"question": h["question"], "answer": h["answer"]} for h in (history or [])][-MAX_HISTORY_TURNS:]
    if mode not in ("answer", "report"):
        raise ValueError("mode must be answer or report")
    system = REPORT_PROMPT if mode == "report" else SYSTEM_PROMPT
    max_steps = max_steps or (REPORT_STEPS if mode == "report" else MAX_STEPS)
    if mode == "report":
        question = f"Write a report on: {question}"
    turn = (model.start(system, TOOL_SPECS, question, history) if history
            else model.start(system, TOOL_SPECS, question))
    for _ in range(max_steps):
        if not turn.tool_calls:
            break
        results = []
        for call in turn.tool_calls:
            if on_event:
                on_event({"type": "lookup", "tool": call.name, "arguments": call.args})
            results.append((call, run_tool(registry, call)))
        steps += results
        turn = model.send_tool_results(results)
    stopped = bool(turn.tool_calls)
    if stopped:
        # Out of lookups: let the model answer from what it already has instead of returning nothing.
        turn = model.send_tool_results([(call, {"error": STEP_LIMIT_NOTE}) for call in turn.tool_calls])
    text = (turn.text or "").strip() or ("I could not finish within the step limit." if stopped
                                         else "The model returned no answer.")
    cited, unsupported = check_citations(text, steps)
    return AgentResult(text, steps, cited, unsupported, stopped, check_attribution(text, steps))


class GeminiModel:
    """Gemini on Vertex AI, with the SDK's automatic tool calling turned off so the loop above runs every tool."""

    def __init__(self, model: str | None = None, project: str | None = None, location: str | None = None):
        project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set. See the setup steps in the README.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError('The Vertex AI SDK is not installed. Run: pip install -e ".[vertex]"') from exc
        self.model = model or os.environ.get("NEUROSCOPE_AGENT_MODEL") or DEFAULT_MODEL
        self._types = types
        # Gemini is served from the "global" location; embeddings use GOOGLE_CLOUD_LOCATION.
        self._client = genai.Client(vertexai=True, project=project,
                                    location=location or os.environ.get("NEUROSCOPE_AGENT_LOCATION", "global"),
                                    http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))
        self._history: list = []
        self._config = None
        self.input_tokens = self.output_tokens = 0
        # When set, text is streamed: each piece is passed here as it arrives.
        self.on_text: Callable[[str], None] | None = None

    def _stream(self) -> ModelTurn:
        """One model turn, streamed. Text pieces go to on_text as they arrive; tool calls are
        collected. Every part is kept, in order, for the conversation history."""
        sent_text = False

        def attempt():
            nonlocal sent_text
            sent_text = False
            parts, calls, usage = [], [], None
            try:
                for chunk in self._hedged_stream():
                    usage = chunk.usage_metadata or usage
                    content = chunk.candidates[0].content if chunk.candidates else None
                    for part in (content.parts if content and content.parts else []):
                        parts.append(part)
                        if part.function_call:
                            calls.append(ToolCall(part.function_call.name, dict(part.function_call.args or {})))
                        elif part.text and not getattr(part, "thought", False) and self.on_text:
                            sent_text = True
                            self.on_text(part.text)
            except Exception as exc:
                if sent_text:            # the reader has seen text: retrying would repeat it
                    exc.retryable = False
                raise
            return parts, calls, usage

        parts, calls, usage = call_with_retry(attempt)
        if usage:
            self.input_tokens += usage.prompt_token_count or 0
            self.output_tokens += usage.candidates_token_count or 0
        if parts:
            self._history.append(self._types.Content(role="model", parts=parts))
        text = "".join(p.text for p in parts if p.text and not getattr(p, "thought", False))
        return ModelTurn(calls, None if calls else text)

    def _hedged_stream(self):
        """Stream one reply. If the first piece has not arrived after HEDGE_AFTER_S, send the same request
        again and continue with whichever starts first; the other is abandoned."""
        history = list(self._history)
        replies: queue.Queue = queue.Queue()

        def open_stream() -> None:
            try:
                it = iter(self._client.models.generate_content_stream(
                    model=self.model, contents=history, config=self._config))
                replies.put((it, next(it, None), None))
            except Exception as exc:
                replies.put((None, None, exc))

        threading.Thread(target=open_stream, daemon=True).start()
        started, failures, wait = 1, [], HEDGE_AFTER_S
        while True:
            try:
                it, first, exc = replies.get(timeout=wait)
            except queue.Empty:
                if started == 1:                        # no reply yet: race a duplicate
                    threading.Thread(target=open_stream, daemon=True).start()
                    started, wait = 2, REQUEST_TIMEOUT_MS / 1000
                    continue
                raise TimeoutError("the model sent nothing back") from None
            if exc is None:
                break
            failures.append(exc)
            if started == 1 or len(failures) == started:
                raise exc                               # let call_with_retry decide
        if first is not None:
            yield first
        yield from it

    def _generate(self) -> ModelTurn:
        from google.genai import errors
        try:
            return self._stream()               # always streamed: see REQUEST_TIMEOUT_MS
        except errors.APIError as exc:
            message = (getattr(exc, "message", None) or str(exc)).split(". If you ")[0]
            raise RuntimeError(f"Vertex AI request failed ({getattr(exc, 'code', '?')} "
                               f"{getattr(exc, 'status', '')}): {message}") from exc
        except Exception as exc:                 # timeouts and connection errors from the HTTP layer
            raise RuntimeError(f"Vertex AI request did not complete: {type(exc).__name__}: {exc}") from exc

    def start(self, system: str, tool_specs: list[dict], question: str,
              history: list[dict] | None = None) -> ModelTurn:
        t = self._types
        declarations = [t.FunctionDeclaration(name=s["name"], description=s["description"],
                                              parameters_json_schema=s["parameters"]) for s in tool_specs]
        budget = int(os.environ.get("NEUROSCOPE_THINKING_BUDGET", THINKING_BUDGET))
        self._config = t.GenerateContentConfig(
            system_instruction=system, temperature=0, tools=[t.Tool(function_declarations=declarations)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            # include_thoughts streams the model's reasoning as it happens, so a call that is thinking keeps
            # sending data and is never mistaken for a stalled one (the timeout is the longest silence).
            # Thought parts are never shown or kept as answer text.
            thinking_config=t.ThinkingConfig(include_thoughts=True,
                                             thinking_budget=None if budget < 0 else budget))
        self._history = []
        for turn in history or []:
            self._history.append(t.Content(role="user", parts=[t.Part(text=turn["question"])]))
            self._history.append(t.Content(role="model", parts=[t.Part(text=turn["answer"])]))
        self._history.append(t.Content(role="user", parts=[t.Part(text=question)]))
        return self._generate()

    def send_tool_results(self, results: list[tuple[ToolCall, dict]]) -> ModelTurn:
        t = self._types
        self._history.append(t.Content(role="user", parts=[
            t.Part.from_function_response(name=call.name, response={"result": result}) for call, result in results]))
        return self._generate()


def format_result(result: AgentResult, show_steps: bool = False) -> str:
    out = []
    if show_steps:
        for n, (call, res) in enumerate(result.steps, start=1):
            summary = "error: " + res["error"] if "error" in res else ", ".join(list(res)[:6])
            out.append(f"[step {n}] {call.name}({json.dumps(call.args)}) -> {summary}")
        out.append("")
    out.append(result.answer)
    cited = sum(len(v) for v in result.cited.values())
    bad = [i for ids in result.unsupported.values() for i in ids]
    out.append("")
    if bad:
        out.append(f"WARNING: {len(bad)} identifier(s) in this answer did not come from any tool result "
                   f"and should not be trusted: {', '.join(bad)}")
    elif cited:
        out.append(f"Citation check: all {cited} identifiers cited above came from database lookups in this run "
                   f"({len(result.steps)} lookups).")
    else:
        out.append(f"Citation check: the answer cites no trial, FDA, or PubMed identifiers "
                   f"({len(result.steps)} lookups were made).")
    if result.stopped_early:
        out.append("Note: the step limit was reached before the model finished.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question")
    p.add_argument("--show-steps", action="store_true", help="print each database lookup the model made")
    p.add_argument("--report", action="store_true", help="write a sectioned report on the topic instead of an answer")
    p.add_argument("--keyword-search", action="store_true",
                   help="use keyword search for literature instead of embeddings")
    args = p.parse_args(argv)
    try:
        model = GeminiModel()
        embedder = None
        if not args.keyword_search:
            from .embed import VertexEmbedder
            embedder = VertexEmbedder()
        with connect() as conn:
            result = answer_question(args.question, model, build_tools(conn, embedder),
                                     mode="report" if args.report else "answer")
    except RuntimeError as exc:
        print(exc)
        return 2
    print(format_result(result, args.show_steps))
    print(f"(model {model.model}; {model.input_tokens} input and {model.output_tokens} output tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
