from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _mean(values: Iterable[float]) -> float | None:
    collected = list(values)
    return statistics.fmean(collected) if collected else None


def load_observations(path: Path) -> list[dict[str, Any]]:
    observations = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"observation line {line_number} must be a JSON object")
            observations.append(value)
    return observations


def summarize_observations(
    observations: list[dict[str, Any]],
    *,
    cutoffs: Iterable[int] = (10, 20, 50, 200),
) -> dict[str, Any]:
    """Summarize content-free retrieval and answer observations without question-specific rules."""
    recall_hits: dict[int, list[float]] = defaultdict(list)
    evidence_coverage: dict[int, list[float]] = defaultdict(list)
    current_hits = 0
    retrieved_candidates = 0
    stale_hits = 0
    stale_opportunities = 0
    answer_scores = []
    abstentions = []
    false_supersessions = []
    fama_scores = []
    state_resolution_scores = []
    retrieved_chars = []
    retrieved_tokens = []
    write_latencies = []
    search_latencies = []
    provider_totals: dict[str, float] = defaultdict(float)

    normalized_cutoffs = sorted({int(cutoff) for cutoff in cutoffs if int(cutoff) > 0})
    for observation in observations:
        candidate_ids = [str(value) for value in observation.get("candidate_ids", [])]
        relevant_ids = {str(value) for value in observation.get("relevant_ids", [])}
        current_ids = {str(value) for value in observation.get("current_relevant_ids", [])}
        stale_ids = {str(value) for value in observation.get("stale_ids", [])}

        direct_evidence_coverage = observation.get("candidate_evidence_coverage_scores") or {}
        if isinstance(direct_evidence_coverage, dict):
            for cutoff, score in direct_evidence_coverage.items():
                cutoff_value = int(cutoff)
                if cutoff_value in normalized_cutoffs and isinstance(score, (int, float)) and 0 <= score <= 1:
                    evidence_coverage[cutoff_value].append(float(score))
        if relevant_ids:
            for cutoff in normalized_cutoffs:
                recall_hits[cutoff].append(len(relevant_ids & set(candidate_ids[:cutoff])) / len(relevant_ids))

        candidate_set = set(candidate_ids)
        current_hits += len(candidate_set & current_ids)
        stale_hits += len(candidate_set & stale_ids)
        if current_ids or stale_ids:
            retrieved_candidates += len(candidate_set)
        stale_opportunities += len(stale_ids)

        if observation.get("answer_score") is not None:
            score = observation["answer_score"]
            if isinstance(score, (int, float)) and 0 <= score <= 1:
                answer_scores.append(float(score))
        elif observation.get("answer_correct") is not None:
            answer_scores.append(float(bool(observation["answer_correct"])))
        if observation.get("abstained") is not None:
            abstentions.append(float(bool(observation["abstained"])))
        if observation.get("false_supersession") is not None:
            false_supersessions.append(float(bool(observation["false_supersession"])))
        if observation.get("state_resolution_correct") is not None:
            state_resolution_scores.append(float(bool(observation["state_resolution_correct"])))
        if observation.get("fama_score") is not None:
            score = observation["fama_score"]
            if isinstance(score, (int, float)) and 0 <= score <= 1:
                fama_scores.append(float(score))

        for key, destination in (
            ("retrieved_chars", retrieved_chars),
            ("retrieved_tokens", retrieved_tokens),
            ("write_latency_ms", write_latencies),
            ("search_latency_ms", search_latencies),
        ):
            value = observation.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                destination.append(float(value))
        latency_samples = observation.get("write_latency_samples_ms")
        if isinstance(latency_samples, list):
            write_latencies.extend(
                float(value) for value in latency_samples if isinstance(value, (int, float)) and value >= 0
            )

        provider_calls = observation.get("provider_calls") or {}
        if isinstance(provider_calls, dict):
            for provider, count in provider_calls.items():
                if isinstance(count, (int, float)) and count >= 0:
                    provider_totals[str(provider)] += float(count)

    latency = {
        "write_ms": {"p50": _percentile(write_latencies, 0.5), "p95": _percentile(write_latencies, 0.95)},
        "search_ms": {"p50": _percentile(search_latencies, 0.5), "p95": _percentile(search_latencies, 0.95)},
    }
    return {
        "schema_version": 1,
        "sample_count": len(observations),
        "candidate_recall_at_k": {str(cutoff): _mean(recall_hits[cutoff]) for cutoff in normalized_cutoffs},
        "candidate_evidence_coverage_at_k": {
            str(cutoff): _mean(evidence_coverage[cutoff]) for cutoff in normalized_cutoffs
        },
        "current_state_precision": current_hits / retrieved_candidates if retrieved_candidates else None,
        "stale_memory_leakage": stale_hits / stale_opportunities if stale_opportunities else None,
        "false_supersession_rate": _mean(false_supersessions),
        "forgetting_aware_memory_accuracy": _mean(fama_scores),
        "state_resolution_accuracy": _mean(state_resolution_scores),
        "answer_accuracy": _mean(answer_scores),
        "abstention_rate": _mean(abstentions),
        "retrieved_context": {
            "mean_chars": _mean(retrieved_chars),
            "mean_tokens": _mean(retrieved_tokens),
        },
        "latency": latency,
        "provider_calls": {
            key: {"total": total, "mean_per_sample": total / len(observations) if observations else None}
            for key, total in sorted(provider_totals.items())
        },
    }


def _nested_get(mapping: dict[str, Any], dotted_path: str) -> Any:
    value: Any = mapping
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def compare_summaries(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    gates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compare two summaries using declarative relative or absolute acceptance gates."""
    results = {}
    passed = True
    for metric, rule in sorted(gates.items()):
        baseline_value = _nested_get(baseline, metric)
        candidate_value = _nested_get(candidate, metric)
        verdict = False
        delta = None
        if isinstance(baseline_value, (int, float)) and isinstance(candidate_value, (int, float)):
            delta = candidate_value - baseline_value
            if "candidate_max" in rule:
                verdict = candidate_value <= float(rule["candidate_max"])
            elif "candidate_min" in rule:
                verdict = candidate_value >= float(rule["candidate_min"])
            elif "relative_max" in rule:
                verdict = candidate_value <= baseline_value * float(rule["relative_max"])
            elif "relative_min" in rule:
                verdict = candidate_value >= baseline_value * float(rule["relative_min"])
            elif "delta_max" in rule:
                verdict = delta <= float(rule["delta_max"])
            elif "delta_min" in rule:
                verdict = delta >= float(rule["delta_min"])
        results[metric] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": delta,
            "passed": verdict,
            "rule": rule,
        }
        passed = passed and verdict
    return {"schema_version": 1, "passed": passed, "gates": results}
