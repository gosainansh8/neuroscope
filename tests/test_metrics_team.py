"""Metrics, tracing no-ops, and the agent-team option in the API. No network: the team is replaced."""

from neuroscope import api, tracing
from neuroscope.agent import AgentResult, ModelTurn, ToolCall
from neuroscope.evaluate import performance
from neuroscope.metrics import ModelCall, Usage
from tests.test_api_db import client, use_model  # noqa: F401  (fixtures)


def test_usage_summary_counts_tokens_time_and_cost(monkeypatch):
    monkeypatch.setenv("NEUROSCOPE_PRICE_INPUT_PER_M", "1.0")
    monkeypatch.setenv("NEUROSCOPE_PRICE_OUTPUT_PER_M", "10.0")
    u = Usage()
    u.add(ModelCall(seconds=3.0, first_piece_seconds=1.0, input_tokens=1000, output_tokens=100, thinking_tokens=100))
    u.add(ModelCall(seconds=2.0, first_piece_seconds=1.0, input_tokens=2000, output_tokens=200, hedged=True))
    s = u.summary(wall_seconds=5.5)
    assert (s["model_calls"], s["hedged_calls"], s["input_tokens"], s["thinking_tokens"]) == (2, 1, 3000, 100)
    assert s["tokens_per_second"] == round(400 / 3.0, 1)       # generated tokens over time after replies started
    assert s["cost_usd"] == round(3000 * 1 / 1e6 + 400 * 10 / 1e6, 6) and s["seconds"] == 5.5


def test_tracing_is_a_no_op_when_off():
    with tracing.span("anything", x=1) as set_attrs:
        set_attrs(y=2)                                           # never raises


def test_evaluation_performance_summary():
    rows = [{"seconds": 10, "metrics": {"cost_usd": 0.01, "tokens_per_second": 50, "model_calls": 3}},
            {"seconds": 20, "metrics": {"cost_usd": 0.03, "tokens_per_second": 70, "model_calls": 5}}]
    p = performance(rows)
    assert (p["median_seconds"], p["median_tokens_per_second"], p["mean_cost_usd"], p["total_cost_usd"],
            p["mean_model_calls"]) == (15, 60, 0.02, 0.04, 4.0)


def test_answers_carry_metrics_and_the_team_option_costs_three(client, conn, monkeypatch):
    use_model([ModelTurn(text="Lecanemab is FDA-approved under BLA999001.")])
    body = client.post("/ask", json={"question": "Is lecanemab approved?"}).json()
    assert body["metrics"]["model_calls"] == 0 and "cost_usd" in body["metrics"]   # the stand-in records no calls

    from neuroscope import team
    seen = {}

    def fake_team(question, registry, on_event=None):
        seen["question"] = question
        call = ToolCall("get_fda_status", {"drug": "lecanemab"})
        steps = [(call, registry["get_fda_status"](drug="lecanemab"))]
        result = AgentResult("Yes, lecanemab is FDA-approved (BLA999001).", steps,
                             {"fda_application": ["BLA999001"]}, {"fda_application": []})
        result.metrics = {"model_calls": 6, "cost_usd": 0.0085, "model_calls_by_agent": {"critic": 1}}
        return result

    monkeypatch.setattr(team, "answer_question_team", fake_team)
    before = conn.execute("SELECT questions FROM api_usage WHERE day = current_date").fetchone()[0]
    t = client.post("/ask", json={"question": "Is lecanemab approved?", "agent": "team"}).json()
    assert seen["question"] == "Is lecanemab approved?" and t["metrics"]["model_calls"] == 6
    assert t["cached"] is False                                  # not the single agent's saved answer
    after = conn.execute("SELECT questions FROM api_usage WHERE day = current_date").fetchone()[0]
    assert after - before == 3
    assert client.post("/ask", json={"question": "x y z", "agent": "crowd"}).status_code == 422
