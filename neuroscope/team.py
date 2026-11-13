"""An agent team built with Google's Agent Development Kit (ADK): an alternative to the single agent.

Usage:
    python -m neuroscope.team "Is opicapone approved for Parkinson's disease?"
    python -m neuroscope.evaluate --agent team

HOW IT WORKS
    coordinator ──▶ regulatory specialist   find_drug, get_fda_status, get_ema_status
        │       ──▶ clinical specialist     find_drug, get_trials, get_program_status, list_pipeline, compare_diseases
        │       ──▶ evidence specialist     find_drug, search_literature, search_disclosures
        ▼
      draft ──▶ critic ──▶ final answer ──▶ citation and attribution check (the same code as the single agent)

The coordinator asks the specialists it needs (each is an ADK agent called as a tool), then writes a
draft. The critic sees the draft and every record the specialists looked up, removes or fixes any claim
or identifier those records do not support, and enforces the answer format. The specialists are the
only agents with database access, and only through the same read-only lookups as the single agent.

The single agent stays the site default: the team makes more model calls, so it is slower and costs
more per answer. `python -m neuroscope.evaluate --agent team` measures both on the same questions.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Callable

from . import tracing
from .agent import (DEFAULT_MODEL, HEDGE_AFTER_S, SYSTEM_PROMPT, THINKING_BUDGET, AgentResult, ToolCall,
                    check_attribution, check_citations, run_tool)
from .metrics import ModelCall, Usage

APP = "neuroscope"
TEAM_TIMEOUT_MS = 60_000
# The specialists fetch facts and the critic checks them against records: they need little hidden reasoning.
# The coordinator plans and writes, so it keeps the single agent's budget.
SPECIALIST_THINKING = 256
CRITIC_THINKING = 512

COORDINATOR = SYSTEM_PROMPT + """

You coordinate a team. You cannot look anything up yourself: ask the specialists, as tools, short
specific questions, and ask each only what it is for. Ask several in one turn when you can.
- regulatory: drug identity, US FDA and EU EMA approval and label or indication text
- clinical: trials, program status (active or discontinued), the pipeline, cross-disease comparisons
- evidence: published abstracts (PubMed) and company SEC filings
Then write the answer from what they returned, following the rules above, citing the identifiers they gave."""

SPECIALIST = """You are the {role} specialist on a drug-intelligence team covering Alzheimer's disease,
Parkinson's disease, and ALS. Answer the coordinator's question using only your lookups. Report the facts
plainly with every identifier exactly as the lookup returned it (NCT IDs, FDA application numbers, EMA
product numbers, SEC accession numbers, PMIDs), quote label or indication text where approval for a disease
matters, and pass on caveats that change the meaning. Say what the lookups did not show. Do not guess."""

CRITIC = """You are the critic. Below are a draft answer and every record the team's lookups returned.

Check the draft sentence by sentence against the records:
1. Remove or correct any claim, number, date, or identifier the records do not support.
2. Approval must come only from FDA or EMA records, never from trial phase; a drug is approved for a
   disease only if label or indication text in the records says so.
3. Keep the format: a one- or two-sentence direct answer (for a yes-or-no question the first word is "Yes"
   or "No"), then at most five short bullets with their identifiers, under about 150 words unless the
   question asks for a list. No headings. Keep identifiers exactly as written in the records.
Return only the final answer text, with no notes about your review.

QUESTION:
{question}

DRAFT:
{draft}

RECORDS (JSON):
{records}"""


def _copy_request(request):
    """A copy safe to send twice: the message list and config are copied, the rest is shared (it holds
    locks and cannot be deep-copied)."""
    update = {"contents": list(request.contents)}
    if getattr(request, "config", None) is not None:
        update["config"] = request.config.model_copy(deep=True)
    return request.model_copy(update=update)


def _hedged_gemini_class():
    """ADK's Gemini model with the single agent's stall protection: if no reply has started after
    HEDGE_AFTER_S, the same request is raced and whichever replies first is used."""
    from google.adk.models import Gemini

    class HedgedGemini(Gemini):
        async def generate_content_async(self, llm_request, stream: bool = False):
            base = Gemini.generate_content_async

            def start(request):
                gen = base(self, request, stream)
                return gen, asyncio.ensure_future(gen.__anext__())

            first_gen, first = start(_copy_request(llm_request))
            done, _ = await asyncio.wait({first}, timeout=HEDGE_AFTER_S)
            if done:
                winner_gen, winner = first_gen, first
            else:
                second_gen, second = start(_copy_request(llm_request))
                pending, winner_gen, winner = {first, second}, None, None
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    ok = [t for t in done if not t.exception()]
                    if ok:
                        winner = ok[0]
                        winner_gen = first_gen if winner is first else second_gen
                        break
                if winner is None:
                    raise next(iter(done)).exception()
                for t in pending:
                    t.cancel()
            yield winner.result()
            async for response in winner_gen:
                yield response

    return HedgedGemini


def _client():
    from google import genai
    from google.genai import types
    return genai.Client(vertexai=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                        location=os.environ.get("NEUROSCOPE_AGENT_LOCATION", "global"),
                        http_options=types.HttpOptions(timeout=TEAM_TIMEOUT_MS,
                                                       retry_options=types.HttpRetryOptions(attempts=3)))


class Team:
    """One question's run: the agents, the lookups they made, and the model calls they used."""

    def __init__(self, registry: dict[str, Callable[..., dict]], on_event: Callable[[dict], None] | None = None):
        from google.adk.agents import LlmAgent
        from google.adk.tools.agent_tool import AgentTool
        from google.genai import types

        self.registry, self.on_event = registry, on_event
        self.steps: list[tuple[ToolCall, dict]] = []
        self.usage = Usage()
        self._started: dict[str, float] = {}
        model = _hedged_gemini_class()(model=os.environ.get("NEUROSCOPE_AGENT_MODEL") or DEFAULT_MODEL,
                                       client=_client())

        def config_with(budget: int):
            return types.GenerateContentConfig(temperature=0,
                                               thinking_config=types.ThinkingConfig(thinking_budget=budget))
        config = config_with(THINKING_BUDGET)
        callbacks = dict(before_model_callback=self._before_model, after_model_callback=self._after_model)

        def specialist(role: str, names: list[str]) -> LlmAgent:
            return LlmAgent(name=role, model=model, generate_content_config=config_with(SPECIALIST_THINKING),
                            description=f"The {role} specialist", instruction=SPECIALIST.format(role=role),
                            tools=[self._tool(n) for n in names], **callbacks)

        self.coordinator = LlmAgent(
            name="coordinator", model=model, generate_content_config=config, instruction=COORDINATOR,
            tools=[AgentTool(specialist("regulatory", ["find_drug", "get_fda_status", "get_ema_status"])),
                   AgentTool(specialist("clinical", ["find_drug", "get_trials", "get_program_status",
                                                     "list_pipeline", "compare_diseases"])),
                   AgentTool(specialist("evidence", ["find_drug", "search_literature", "search_disclosures"]))],
            **callbacks)
        self.critic = LlmAgent(name="critic", model=model, generate_content_config=config_with(CRITIC_THINKING),
                               instruction="Follow the user's instructions exactly.", **callbacks)

    # Each lookup is an ordinary Python function for ADK (it reads the parameters from the signature and
    # the description from the docstring); it runs the same registry as the single agent and records the
    # call so the citation check can see every record.
    def _call(self, tool_name: str, /, **kwargs) -> dict:
        args = {k: v for k, v in kwargs.items() if v is not None}
        call = ToolCall(tool_name, args)
        if self.on_event:
            self.on_event({"type": "lookup", "tool": tool_name, "arguments": args})
        result = run_tool(self.registry, call)
        self.steps.append((call, result))
        return result

    def _tool(self, name: str):
        team = self

        def find_drug(name: str) -> dict:
            """Resolve a drug name (generic, brand, or code) to its canonical drug, verified names, and trial
            counts by disease."""
            return team._call("find_drug", name=name)

        def get_fda_status(drug: str, disease: str | None = None) -> dict:
            """US FDA approval records and label indication text for one drug. disease: alzheimers, parkinsons,
            or als, to show the label that mentions it."""
            return team._call("get_fda_status", drug=drug, disease=disease)

        def get_ema_status(drug: str, disease: str | None = None) -> dict:
            """European Medicines Agency records for one drug (authorised, refused, withdrawn) with indication
            text. disease: alzheimers, parkinsons, or als."""
            return team._call("get_ema_status", drug=drug, disease=disease)

        def get_trials(drug: str, disease: str | None = None, limit: int | None = None) -> dict:
            """Clinical trial counts and the most relevant trials for one drug, in one disease (alzheimers,
            parkinsons, als) or all."""
            return team._call("get_trials", drug=drug, disease=disease, limit=limit)

        def get_program_status(drug: str | None = None, disease: str | None = None, status: str | None = None,
                               min_phase: float | None = None) -> dict:
            """Whether a drug's program in each disease is active, discontinued, dormant, or completed, with the
            latest stopped trial and its reason. Without a drug, lists programs with a status in a disease."""
            return team._call("get_program_status", drug=drug, disease=disease, status=status, min_phase=min_phase)

        def list_pipeline(min_phase: float | None = None, active_only: bool | None = None,
                          disease: str | None = None, limit: int | None = None) -> dict:
            """Drugs by highest trial phase, in one disease or all, with US and EU approval flags."""
            return team._call("list_pipeline", min_phase=min_phase, active_only=active_only, disease=disease,
                              limit=limit)

        def compare_diseases(aspect: str, min_phase: float | None = None, active_only: bool | None = None,
                             primary_only: bool | None = None) -> dict:
            """Compare Alzheimer's, Parkinson's, and ALS: aspect is mechanisms, endpoints, or biomarkers."""
            return team._call("compare_diseases", aspect=aspect, min_phase=min_phase, active_only=active_only,
                              primary_only=primary_only)

        def search_literature(query: str, drug: str | None = None, limit: int | None = None) -> dict:
            """Search stored PubMed abstracts by meaning and keywords, optionally for one drug."""
            return team._call("search_literature", query=query, drug=drug, limit=limit)

        def search_disclosures(query: str, drug: str | None = None, limit: int | None = None) -> dict:
            """Search company press releases and annual reports filed with the SEC, optionally for one drug."""
            return team._call("search_disclosures", query=query, drug=drug, limit=limit)

        return locals()[name]

    def _before_model(self, ctx, request):
        self._started[ctx.agent_name] = time.monotonic()
        if self.on_event and ctx.agent_name == "critic":
            self.on_event({"type": "stage", "text": "Critic reviewing the draft"})

    def _after_model(self, ctx, response):
        u = response.usage_metadata
        if u is None:
            return None
        self.usage.add(ModelCall(seconds=time.monotonic() - self._started.get(ctx.agent_name, time.monotonic()),
                                 first_piece_seconds=None, input_tokens=u.prompt_token_count or 0,
                                 output_tokens=u.candidates_token_count or 0,
                                 thinking_tokens=getattr(u, "thoughts_token_count", 0) or 0, agent=ctx.agent_name))
        return None

    async def _run(self, agent, text: str) -> str:
        from google.adk.runners import InMemoryRunner
        from google.genai import types
        runner = InMemoryRunner(agent=agent, app_name=APP)
        session = await runner.session_service.create_session(app_name=APP, user_id="neuroscope")
        final = ""
        async for event in runner.run_async(user_id="neuroscope", session_id=session.id,
                                            new_message=types.Content(role="user", parts=[types.Part(text=text)])):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts if not getattr(p, "thought", False))
        return final.strip()

    async def answer(self, question: str) -> str:
        draft = await self._run(self.coordinator, question)
        records = json.dumps([{"lookup": c.name, "arguments": c.args, "result": r} for c, r in self.steps],
                             default=str)[:60_000]
        return await self._run(self.critic, CRITIC.format(question=question, draft=draft or "(no draft)",
                                                          records=records)) or draft


def answer_question_team(question: str, registry: dict[str, Callable[..., dict]],
                         on_event: Callable[[dict], None] | None = None) -> AgentResult:
    """Answer with the agent team. Same result shape and checks as agent.answer_question."""
    started = time.monotonic()
    team = Team(registry, on_event)
    with tracing.span("answer", mode="answer", agent="team", question_chars=len(question)) as set_attrs:
        try:
            text = asyncio.run(team.answer(question))
        except Exception as exc:              # surface model failures like the single agent does
            raise RuntimeError(f"Agent team failed: {type(exc).__name__}: {exc}") from exc
        text = text or "The agent team returned no answer."
        cited, unsupported = check_citations(text, team.steps)
        result = AgentResult(text, team.steps, cited, unsupported, False, check_attribution(text, team.steps))
        result.metrics = team.usage.summary(time.monotonic() - started)
        result.metrics["model_calls_by_agent"] = {
            a: sum(1 for c in team.usage.calls if c.agent == a) for a in sorted({c.agent for c in team.usage.calls})}
        set_attrs(**{k: v for k, v in result.metrics.items() if isinstance(v, (int, float))},
                  lookups=len(team.steps), citations_verified=not any(unsupported.values()))
    return result


def main(argv: list[str] | None = None) -> int:
    from .agent import build_tools, format_result
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    from .embed import VertexEmbedder
    with connect() as conn:
        result = answer_question_team(" ".join(argv), build_tools(conn, VertexEmbedder()))
    print(format_result(result, show_steps=True))
    print(result.metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
