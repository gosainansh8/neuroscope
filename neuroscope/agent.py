"""A question-answering agent over the NeuroScope database.

Usage:
    python -m neuroscope.agent "What is the regulatory status of lecanemab?"
    python -m neuroscope.agent "Which drugs have active Phase 3 trials but no FDA record?" --show-steps

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
    and PMID in the answer must appear in a tool result from this run. Any that
    do not are reported as unsupported. The check cannot tell whether a correctly
    cited source actually supports the sentence; that still needs a reader.

The model is Gemini on Vertex AI (NEUROSCOPE_AGENT_MODEL, default below; served
from NEUROSCOPE_AGENT_LOCATION, default "global"). Tests
use a scripted stand-in model, so they need no network.
"""

from __future__ import annotations

import argparse
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
REQUEST_TIMEOUT_MS = 45_000               # a stalled request fails (and is retried) instead of hanging

SYSTEM_PROMPT = """You answer questions about Alzheimer's disease drug development using the NeuroScope database.

Rules:
1. Use only facts returned by the tools in this conversation. Do not use your own memory for any fact about a drug, trial, approval, or paper. If the tools do not answer the question, say what is missing.
2. Cite the source of every factual claim with its identifier exactly as the tool returned it: trial IDs (NCT...), FDA application numbers (NDA..., BLA..., ANDA...), and PubMed IDs written as "PMID 12345678". Never write an identifier that a tool did not return.
3. Approval status comes only from get_fda_status. Never infer approval, or lack of it, from trial phase, trial status, or completion. If there is no FDA record, say "no matched FDA approval record" and that this is not proof the drug is unapproved.
4. "FDA-approved" means approved for at least one use. Say a drug is approved for Alzheimer's disease only if the label text returned by the tool says so, and quote the relevant words.
5. Give dates as the tools give them, and say which source date applies when it matters.
6. Pass on the caveats that come with tool results when they affect the answer.
7. Be concise and plain. Start with the direct answer, then the supporting facts with their citations.
8. Earlier questions and answers in this conversation are context for understanding a follow-up (for example, what "it" refers to). They are not evidence: look up again, in this turn, any fact you cite."""

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "find_drug",
     "description": "Resolve a drug name (generic, brand, or code) to its canonical drug and verified names. "
                    "Call this first when a question names a drug.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_trials",
     "description": "Clinical trial counts and the most relevant trials for one drug, from ClinicalTrials.gov.",
     "parameters": {"type": "object", "properties": {
         "drug": {"type": "string"}, "limit": {"type": "integer", "description": "trials to list, default 8"}},
         "required": ["drug"]}},
    {"name": "get_fda_status",
     "description": "US FDA approval records and label indication text for one drug. The only source of "
                    "approval status.",
     "parameters": {"type": "object", "properties": {"drug": {"type": "string"}}, "required": ["drug"]}},
    {"name": "search_literature",
     "description": "Search stored PubMed abstracts by meaning. Optionally restrict to one drug.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "drug": {"type": "string"},
         "limit": {"type": "integer", "description": "results, default 5"}}, "required": ["query"]}},
    {"name": "list_pipeline",
     "description": "List drugs by highest trial phase, with whether each has an FDA approval record.",
     "parameters": {"type": "object", "properties": {
         "min_phase": {"type": "number", "description": "1 to 4, default 3"},
         "active_only": {"type": "boolean", "description": "count only active trials, default true"},
         "limit": {"type": "integer"}}}},
]

_ID_PATTERNS = {
    "nct": re.compile(r"\bNCT\d{8}\b"),
    # Models also write "NDA 209637" or "NDA-209637"; both are read as NDA209637.
    "fda_application": re.compile(r"\b(NDA|BLA|ANDA)[\s-]?(\d{6})\b"),
    "pmid": re.compile(r"(?:PMID:?\s*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{6,9})"),
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
        "get_trials": lambda drug, limit=8: tools.get_trials(conn, drug, min(int(limit), 15)),
        "get_fda_status": lambda drug: tools.get_fda_status(conn, drug),
        "search_literature": lambda query, drug=None, limit=5: tools.search_literature(
            conn, query, drug or None, min(int(limit), 8), embedder),
        "list_pipeline": lambda min_phase=3, active_only=True, limit=25: tools.list_pipeline(
            conn, float(min_phase), bool(active_only), min(int(limit), 40)),
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


def answer_question(question: str, model: ChatModel, registry: dict[str, Callable[..., dict]],
                    max_steps: int = MAX_STEPS, history: list[dict] | None = None) -> AgentResult:
    """Answer one question. `history` holds earlier {"question", "answer"} turns of the same
    conversation (oldest first, at most MAX_HISTORY_TURNS kept) so follow-ups can be understood.
    Citations are still checked only against lookups made in this turn."""
    steps: list[tuple[ToolCall, dict]] = []
    history = [{"question": h["question"], "answer": h["answer"]} for h in (history or [])][-MAX_HISTORY_TURNS:]
    turn = (model.start(SYSTEM_PROMPT, TOOL_SPECS, question, history) if history
            else model.start(SYSTEM_PROMPT, TOOL_SPECS, question))
    for _ in range(max_steps):
        if not turn.tool_calls:
            break
        results = [(call, run_tool(registry, call)) for call in turn.tool_calls]
        steps += results
        turn = model.send_tool_results(results)
    stopped = bool(turn.tool_calls)
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

    def _generate(self) -> ModelTurn:
        from google.genai import errors
        try:
            resp = call_with_retry(lambda: self._client.models.generate_content(
                model=self.model, contents=self._history, config=self._config))
        except errors.APIError as exc:
            message = (getattr(exc, "message", None) or str(exc)).split(". If you ")[0]
            raise RuntimeError(f"Vertex AI request failed ({getattr(exc, 'code', '?')} "
                               f"{getattr(exc, 'status', '')}): {message}") from exc
        except Exception as exc:                 # timeouts and connection errors from the HTTP layer
            raise RuntimeError(f"Vertex AI request did not complete: {type(exc).__name__}: {exc}") from exc
        usage = resp.usage_metadata
        if usage:
            self.input_tokens += usage.prompt_token_count or 0
            self.output_tokens += usage.candidates_token_count or 0
        if resp.candidates and resp.candidates[0].content:
            self._history.append(resp.candidates[0].content)      # kept whole, as the API requires
        calls = [ToolCall(fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
        return ModelTurn(calls, None if calls else resp.text)

    def start(self, system: str, tool_specs: list[dict], question: str,
              history: list[dict] | None = None) -> ModelTurn:
        t = self._types
        declarations = [t.FunctionDeclaration(name=s["name"], description=s["description"],
                                              parameters_json_schema=s["parameters"]) for s in tool_specs]
        self._config = t.GenerateContentConfig(
            system_instruction=system, temperature=0, tools=[t.Tool(function_declarations=declarations)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
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
            result = answer_question(args.question, model, build_tools(conn, embedder))
    except RuntimeError as exc:
        print(exc)
        return 2
    print(format_result(result, args.show_steps))
    print(f"(model {model.model}; {model.input_tokens} input and {model.output_tokens} output tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
