"""Score the agent against questions with known answers.

Usage:
    python -m neuroscope.evaluate                       # every case in evals/cases.json
    python -m neuroscope.evaluate --only fda-lecanemab,ema-kisunla   # some cases
    python -m neuroscope.evaluate --category trials

Each case states what a correct answer must contain, using patterns and
identifiers taken from the database, so scoring needs no model:
  verdict         "yes" or "no": the answer's opening must say so
  must_mention    every pattern must appear (case-insensitive regular expressions)
  must_not_mention  no pattern may appear
  must_cite_any   at least one of these identifiers must be cited
  min_pmids       at least this many PubMed IDs must be cited
  must_call       these lookups must have been made
  expect_missing  the answer must say the data does not cover the question
Every case also fails if the answer cites an identifier that no lookup returned.
Attribution warnings (a citation next to a different drug) are reported but do
not fail a case, because the check is a heuristic.

Results go to evals/results/<timestamp>.json and evals/results/latest.md.
The cases were written for one data snapshot; after a data refresh some
expected identifiers or dates may need updating.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .agent import GeminiModel, answer_question, build_tools
from .db import ROOT, connect

CASES = ROOT / "evals" / "cases.json"
RESULTS = ROOT / "evals" / "results"

_NO = re.compile(r"\bnot (?:fda[- ]?)?approved|no matched fda approval record|no fda approval|has not been approved|"
                 r"is not (?:currently )?(?:fda[- ]?)?approved|not been (?:fda[- ]?)?approved|there is no .{0,30}approval",
                 re.IGNORECASE)
_YES = re.compile(r"\b(?:is|was|has been|been) (?:fda[- ]?)?approved|\bfda[- ]approved\b|approved for the treatment",
                  re.IGNORECASE)
_MISSING = re.compile(
    r"(?:does not|doesn't|do not|cannot|can't|is not able to|unable to) (?:track|contain|include|hold|have|cover|provide|"
    r"answer|determine|recommend|rank|say|compare)|not (?:available|included|tracked|covered|in the database)|"
    r"no (?:data|information|records?) (?:on|about|for|in)|outside (?:the|of the)|only (?:contains|covers|includes|holds|has)|"
    r"(?:is|are) missing|not medical advice", re.IGNORECASE)


def opening(answer: str) -> str:
    """The first sentence or line, without Markdown markers."""
    text = re.sub(r"[*#>_`]", "", answer).strip()
    first = re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0]
    return first[:300]


def verdict(answer: str) -> str | None:
    head = opening(answer)
    if re.match(r"\W*no\b", head, re.IGNORECASE):
        return "no"
    if re.match(r"\W*yes\b", head, re.IGNORECASE):
        return "yes"
    if _NO.search(head):
        return "no"
    if _YES.search(head):
        return "yes"
    return None


def score(case: dict, result) -> dict:
    """Return {check name: (passed, detail)} for one case."""
    text = result.answer
    cited = {i for ids in result.cited.values() for i in ids}
    tools = {c.name for c, _ in result.steps}
    checks: dict[str, tuple[bool, str]] = {}
    if "verdict" in case:
        got = verdict(text)
        checks["verdict"] = (got == case["verdict"], f"expected {case['verdict']}, got {got or 'unclear'}")
    for pattern in case.get("must_mention", []):
        checks[f"mentions /{pattern}/"] = (bool(re.search(pattern, text, re.IGNORECASE)), "")
    for pattern in case.get("must_not_mention", []):
        checks[f"avoids /{pattern}/"] = (not re.search(pattern, text, re.IGNORECASE), "")
    if case.get("must_cite_any"):
        hit = sorted(cited & set(case["must_cite_any"]))
        checks["cites an expected record"] = (bool(hit), ", ".join(hit) or "none of " + ", ".join(case["must_cite_any"]))
    if case.get("min_pmids"):
        n = len(result.cited.get("pmid", []))
        checks[f"cites {case['min_pmids']}+ PubMed IDs"] = (n >= case["min_pmids"], f"{n} cited")
    for tool in case.get("must_call", []):
        checks[f"called {tool}"] = (tool in tools, "")
    if case.get("expect_missing"):
        checks["says the data does not cover it"] = (bool(_MISSING.search(text)), "")
    bad = [i for ids in result.unsupported.values() for i in ids]
    checks["no unsupported citations"] = (not bad, ", ".join(bad))
    return checks


def run(cases: list[dict], model_factory=GeminiModel, embedder=None, pause_s: float = 1.0,
        agent: str = "single") -> list[dict]:
    rows = []
    with connect() as conn:
        tools = build_tools(conn, embedder)
        for n, case in enumerate(cases, start=1):
            model = model_factory()
            started = time.monotonic()
            try:
                if agent == "team":
                    from .team import answer_question_team
                    result = answer_question_team(case["question"], tools)
                    model = result
                else:
                    result = answer_question(case["question"], model, tools)
                error = None
            except RuntimeError as exc:
                result, error = None, str(exc)
            seconds = round(time.monotonic() - started, 1)
            if result is None:
                checks = {"answered": (False, error)}
            else:
                checks = score(case, result)
            passed = all(ok for ok, _ in checks.values())
            rows.append({
                "id": case["id"], "category": case["category"], "question": case["question"], "passed": passed,
                "checks": {k: {"passed": ok, "detail": d} for k, (ok, d) in checks.items()},
                "answer": result.answer if result else None,
                "lookups": [{"tool": c.name, "arguments": c.args} for c, _ in result.steps] if result else [],
                "attribution_warnings": result.attribution_warnings if result else [],
                "seconds": seconds,
                "input_tokens": getattr(model, "input_tokens", None), "output_tokens": getattr(model, "output_tokens", None),
                "metrics": (result.metrics if result else
                            (model.usage.summary(seconds) if hasattr(model, "usage") else {})),
            })
            print(f"[{n}/{len(cases)}] {'PASS' if passed else 'FAIL'} {case['id']} ({seconds}s)"
                  + "".join(f"\n      - {k}: {d}" for k, (ok, d) in checks.items() if not ok), flush=True)
            time.sleep(pause_s)
    return rows


def _median(values: list[float]) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def performance(rows: list[dict]) -> dict:
    """Median seconds and tokens/s, mean and total estimated cost, model calls per answer."""
    m = [r.get("metrics") or {} for r in rows]
    costs = [x.get("cost_usd") for x in m if x.get("cost_usd") is not None]
    med_s, med_tps = _median([r["seconds"] for r in rows]), _median([x.get("tokens_per_second") for x in m])
    return {"median_seconds": round(med_s, 1) if med_s is not None else None,
            "median_tokens_per_second": round(med_tps, 1) if med_tps is not None else None,
            "mean_cost_usd": round(sum(costs) / len(costs), 5) if costs else None,
            "total_cost_usd": round(sum(costs), 4) if costs else None,
            "mean_model_calls": round(sum(x.get("model_calls", 0) for x in m) / len(m), 1) if m else None}


def summarize(rows: list[dict], model_name: str) -> str:
    total, passed = len(rows), sum(r["passed"] for r in rows)
    checks = [c for r in rows for c in r["checks"].values()]
    unsupported = sum(1 for r in rows for k, c in r["checks"].items() if k == "no unsupported citations" and not c["passed"])
    attribution = sum(len(r["attribution_warnings"]) for r in rows)
    tokens_in = sum(r["input_tokens"] or 0 for r in rows)
    tokens_out = sum(r["output_tokens"] or 0 for r in rows)
    by_cat: dict[str, list[bool]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    lines = [
        f"# Agent evaluation, {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Model `{model_name}`. **{passed} of {total} cases passed**; "
        f"{sum(c['passed'] for c in checks)} of {len(checks)} individual checks passed.",
        f"Answers with an unsupported (invented) citation: {unsupported}. Attribution warnings: {attribution}.",
        f"Tokens: {tokens_in:,} input, {tokens_out:,} output.",
        "Median answer time {median_seconds} s; median {median_tokens_per_second} generated tokens/s; "
        "{mean_model_calls} model calls and ${mean_cost_usd} per answer on average (${total_cost_usd} in all, "
        "estimated from token counts).".format(**performance(rows)),
        "",
        "| Category | Passed |",
        "|---|---|",
    ]
    lines += [f"| {cat} | {sum(v)} of {len(v)} |" for cat, v in by_cat.items()]
    failures = [r for r in rows if not r["passed"]]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            missed = [f"{k} ({c['detail']})" if c["detail"] else k for k, c in r["checks"].items() if not c["passed"]]
            lines.append(f"- **{r['id']}**: {r['question']}  \n  Failed: {'; '.join(missed)}")
    if attribution:
        lines += ["", "## Attribution warnings", ""]
        for r in rows:
            for w in r["attribution_warnings"]:
                lines.append(f"- {r['id']}: {w['identifier']} belongs to {', '.join(w['record_belongs_to'])}, "
                             f"but its sentence names {', '.join(w['sentence_names'])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", help="run cases by id, comma-separated")
    p.add_argument("--category")
    p.add_argument("--agent", choices=["single", "team"], default="single",
                   help="single: one agent (the site default); team: the ADK multi-agent version")
    p.add_argument("--keyword-search", action="store_true", help="literature by keyword instead of embeddings")
    args = p.parse_args(argv)
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = {i.strip() for i in args.only.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if not cases:
        print("No matching cases.")
        return 2
    try:
        embedder = None
        if not args.keyword_search:
            from .embed import VertexEmbedder
            embedder = VertexEmbedder()
        model_name = GeminiModel().model
        rows = run(cases, embedder=embedder, agent=args.agent)
    except RuntimeError as exc:
        print(exc)
        return 2
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "" if args.agent == "single" else f"-{args.agent}"
    (RESULTS / f"{stamp}{suffix}.json").write_text(json.dumps(
        {"model": model_name, "agent": args.agent, "performance": performance(rows), "rows": rows}, indent=1),
        encoding="utf-8")
    summary = summarize(rows, model_name + ("" if args.agent == "single" else f", {args.agent} agent"))
    if not (args.only or args.category):
        (RESULTS / f"latest{suffix}.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    return 0 if all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
