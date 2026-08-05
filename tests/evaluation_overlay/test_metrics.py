import json

import pytest

from evaluation_overlay.metrics import compare_summaries, load_observations, summarize_observations


def test_summary_reports_state_quality_cost_latency_and_calls_without_content(tmp_path):
    observations = [
        {
            "question_id": "q1",
            "candidate_ids": ["current", "stale"],
            "relevant_ids": ["current"],
            "current_relevant_ids": ["current"],
            "stale_ids": ["stale", "other-stale"],
            "answer_correct": True,
            "abstained": False,
            "false_supersession": False,
            "state_resolution_correct": True,
            "retrieved_chars": 120,
            "retrieved_tokens": 30,
            "write_latency_ms": 10,
            "search_latency_ms": 20,
            "provider_calls": {"write_llm": 1, "embedding": 2},
        },
        {
            "question_id": "q2",
            "candidate_ids": ["irrelevant"],
            "relevant_ids": ["missing"],
            "current_relevant_ids": ["missing"],
            "stale_ids": [],
            "answer_correct": False,
            "abstained": True,
            "false_supersession": True,
            "state_resolution_correct": False,
            "retrieved_chars": 80,
            "retrieved_tokens": 20,
            "write_latency_ms": 30,
            "search_latency_ms": 40,
            "provider_calls": {"write_llm": 1, "embedding": 3},
        },
    ]
    path = tmp_path / "observations.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in observations), encoding="utf-8")

    summary = summarize_observations(load_observations(path), cutoffs=(1, 2))

    assert summary["candidate_recall_at_k"] == {"1": 0.5, "2": 0.5}
    assert summary["current_state_precision"] == pytest.approx(1 / 3)
    assert summary["stale_memory_leakage"] == 0.5
    assert summary["false_supersession_rate"] == 0.5
    assert summary["state_resolution_accuracy"] == 0.5
    assert summary["answer_accuracy"] == 0.5
    assert summary["abstention_rate"] == 0.5
    assert summary["retrieved_context"] == {"mean_chars": 100.0, "mean_tokens": 25.0}
    assert summary["latency"]["search_ms"] == {"p50": 30.0, "p95": pytest.approx(39.0)}
    assert summary["provider_calls"]["write_llm"] == {"total": 2.0, "mean_per_sample": 1.0}
    serialized = json.dumps(summary).casefold()
    assert "question_id" not in serialized
    assert "candidate_ids" not in serialized
    assert '"q1"' not in serialized
    assert '"irrelevant"' not in serialized
    assert '"other-stale"' not in serialized


def test_compare_summaries_applies_declared_acceptance_gates():
    baseline = {
        "stale_memory_leakage": 0.4,
        "answer_accuracy": 0.8,
        "latency": {"search_ms": {"p95": 100.0}},
    }
    candidate = {
        "stale_memory_leakage": 0.2,
        "answer_accuracy": 0.795,
        "latency": {"search_ms": {"p95": 114.0}},
    }

    comparison = compare_summaries(
        baseline,
        candidate,
        {
            "answer_accuracy": {"delta_min": -0.01},
            "latency.search_ms.p95": {"relative_max": 1.15},
            "stale_memory_leakage": {"relative_max": 0.5},
        },
    )

    assert comparison["passed"] is True
    assert all(result["passed"] for result in comparison["gates"].values())


def test_compare_summaries_fails_closed_for_missing_metrics():
    comparison = compare_summaries({}, {}, {"answer_accuracy": {"candidate_min": 0.5}})

    assert comparison["passed"] is False
    assert comparison["gates"]["answer_accuracy"]["passed"] is False


def test_evidence_coverage_surrogate_never_populates_candidate_recall():
    summary = summarize_observations(
        [{"question_id": "q1", "candidate_evidence_coverage_scores": {"10": 1.0}}],
        cutoffs=(10,),
    )

    assert summary["candidate_recall_at_k"] == {"10": None}
    assert summary["candidate_evidence_coverage_at_k"] == {"10": 1.0}
