"""Per-answer model metrics: calls, tokens (including thinking), time, tokens per second, and cost.

Prices are Vertex AI's for gemini-3.8-flash: $0.75 per million input tokens and $3.75 per million
output tokens (thinking is billed as output), the introductory rates through 2026-12-31. From
2027 the standard rates are $1.50 and $7.50. Override with NEUROSCOPE_PRICE_INPUT_PER_M and
NEUROSCOPE_PRICE_OUTPUT_PER_M. The cost is an estimate from token counts; the bill is the record.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def prices() -> tuple[float, float]:
    return (float(os.environ.get("NEUROSCOPE_PRICE_INPUT_PER_M", 0.75)),
            float(os.environ.get("NEUROSCOPE_PRICE_OUTPUT_PER_M", 3.75)))


@dataclass
class ModelCall:
    seconds: float                     # from sending the request to the last piece of the reply
    first_piece_seconds: float | None  # time until the reply started (includes any stall)
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    hedged: bool = False               # a duplicate request was raced against a stalled one
    agent: str | None = None           # which agent made the call (agent-team mode)


@dataclass
class Usage:
    calls: list[ModelCall] = field(default_factory=list)

    def add(self, call: ModelCall) -> None:
        self.calls.append(call)

    def summary(self, wall_seconds: float | None = None) -> dict:
        p_in, p_out = prices()
        inp = sum(c.input_tokens for c in self.calls)
        out = sum(c.output_tokens for c in self.calls)
        think = sum(c.thinking_tokens for c in self.calls)
        model_s = sum(c.seconds for c in self.calls)
        gen_s = sum(max(c.seconds - (c.first_piece_seconds or 0), 0.0) for c in self.calls)
        return {
            "model_calls": len(self.calls),
            "hedged_calls": sum(c.hedged for c in self.calls),
            "input_tokens": inp, "output_tokens": out, "thinking_tokens": think,
            "model_seconds": round(model_s, 2),
            "seconds": round(wall_seconds, 2) if wall_seconds is not None else round(model_s, 2),
            # generated tokens per second of generation, after each reply started
            "tokens_per_second": round((out + think) / gen_s, 1) if gen_s > 0 else None,
            "cost_usd": round(inp * p_in / 1e6 + (out + think) * p_out / 1e6, 6),
        }
