"""Tests for the content-free benchmark scorecard (Phase A instrumentation)."""
from __future__ import annotations

import json


def test_scorecard_round_trip(tmp_path):
    from evaluation_instrument.scorecard import Scorecard

    card = Scorecard(
        benchmark="locomo",
        arm="vanilla",
        project_name="p",
        mem0_host="http://localhost:8888",
        model="m",
        scores={"f1": 0.5},
        question_count=40,
    )
    path = card.write(tmp_path)
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["arm"] == "vanilla"
    assert "memory_bodies" not in data  # content-free guarantee
    assert path.name == "locomo-vanilla-scorecard.json"
