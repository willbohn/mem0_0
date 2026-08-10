"""Compare a vanilla and candidate scorecard; flag regressions.

A metric regresses when the candidate scores strictly lower than vanilla.
A metric absent from the candidate is reported with a null delta and does NOT
count as a regression (it is an instrumentation gap, not a behavioral one).
"""
from __future__ import annotations

from typing import Any

from evaluation_instrument.scorecard import Scorecard


def diff(vanilla: Scorecard, candidate: Scorecard) -> dict[str, Any]:
    if vanilla.benchmark != candidate.benchmark:
        raise ValueError("cannot diff scorecards across different benchmarks")
    scores: dict[str, Any] = {}
    regressed = False
    for key, v_val in vanilla.scores.items():
        c_val = candidate.scores.get(key)
        delta = None if c_val is None else round(c_val - v_val, 6)
        if delta is not None and delta < 0:
            regressed = True
        scores[key] = {"vanilla": v_val, "candidate": c_val, "delta": delta}
    return {
        "schema_version": 1,
        "benchmark": vanilla.benchmark,
        "scores": scores,
        "regressed": regressed,
    }
