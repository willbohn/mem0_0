from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any


def memory_fingerprint(memory: Any) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", str(memory)).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def candidate_score(result: dict[str, Any]) -> dict[str, float]:
    scores = {}
    for key in ("score", "rerank_score"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scores[key] = float(value)
    return scores


def _select_cutoff(metadata: dict[str, Any], requested: str | None) -> str:
    if requested:
        return requested if requested.startswith("top_") else f"top_{requested}"
    cutoffs = metadata.get("top_k_cutoffs") or []
    if not cutoffs:
        raise ValueError("benchmark result has no declared top-k cutoffs")
    return max((str(value) for value in cutoffs), key=lambda value: int(value.removeprefix("top_")))


def normalize_upstream_result(
    result: dict[str, Any],
    *,
    cutoff: str | None = None,
    run_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Remove raw content while retaining generic retrieval, answer, cost, and latency observations."""
    metadata = result.get("metadata") or {}
    label = _select_cutoff(metadata, cutoff)
    seen: dict[str, dict[str, Any]] = {}
    for evaluation in result.get("evaluations", []):
        if not isinstance(evaluation, dict) or not evaluation.get("question_id"):
            continue
        if label not in (evaluation.get("cutoff_results") or {}):
            continue
        seen[str(evaluation["question_id"])] = evaluation
    if not seen:
        raise ValueError(f"benchmark result has no evaluations for selected cutoff {label}")

    telemetry = (run_state or {}).get("telemetry") or {}
    observations = []
    for index, (question_id, evaluation) in enumerate(sorted(seen.items())):
        retrieval = evaluation.get("retrieval") or {}
        search_results = retrieval.get("search_results") or []
        cutoff_result = evaluation["cutoff_results"][label]
        answer_score = float(cutoff_result.get("score", 0))
        memories = [str(item.get("memory", "")) for item in search_results if isinstance(item, dict)]
        observation: dict[str, Any] = {
            "question_id": question_id,
            "candidate_ids": [
                str(item.get("id")) for item in search_results if isinstance(item, dict) and item.get("id")
            ],
            "candidate_fingerprints": [
                memory_fingerprint(item.get("memory", "")) for item in search_results if isinstance(item, dict)
            ],
            "candidate_scores": [candidate_score(item) for item in search_results if isinstance(item, dict)],
            "answer_score": answer_score,
            "retrieved_chars": sum(len(memory) for memory in memories),
            "retrieved_tokens": sum(len(memory.split()) for memory in memories),
            "search_latency_ms": float(retrieval.get("search_latency_ms", evaluation.get("search_latency_ms", 0)) or 0),
        }
        if telemetry.get("add_requests") and not telemetry.get("write_latency_samples_ms"):
            observation["write_latency_ms"] = float(telemetry.get("write_latency_ms", 0)) / float(
                telemetry["add_requests"]
            )
        if index == 0 and telemetry.get("write_latency_samples_ms"):
            observation["write_latency_samples_ms"] = telemetry["write_latency_samples_ms"]
        if index == 0:
            observation["provider_calls"] = {
                "embedding": float(telemetry.get("embedding_calls", 0)),
                "write_llm": float(telemetry.get("llm_calls", 0)),
            }
        if evaluation.get("is_abstention") or evaluation.get("question_type") == "abstention":
            observation["abstained"] = answer_score >= 0.5
        observations.append(observation)
    return observations


def load_unified_result(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("evaluations"), list):
        raise ValueError(f"{path} is not a unified benchmark result")
    return value


def normalize_stale_judge(result: dict[str, Any]) -> list[dict[str, Any]]:
    observations = []
    for detail in result.get("details", []):
        uid = str(detail.get("uid", ""))
        evaluation = detail.get("evaluation") or {}
        for dimension in ("dim1", "dim2", "dim3"):
            value = evaluation.get(f"{dimension}_eval") or {}
            observations.append(
                {
                    "question_id": f"{uid}:{dimension}",
                    "answer_correct": bool(value.get("pass", False)),
                    "state_resolution_correct": bool(value.get("pass", False)),
                }
            )
    return observations


def normalize_memora_judge(result: dict[str, Any]) -> list[dict[str, Any]]:
    observations = []
    for item in result.get("results", []):
        total = int(item.get("total_eval_questions", 0) or 0)
        correct = int(item.get("memory_presence_correct", 0) or 0) + int(item.get("forgetting_absence_correct", 0) or 0)
        observations.append(
            {
                "question_id": str(item.get("question_id", "")),
                "answer_score": correct / total if total else 0.0,
                "fama_score": float(item.get("fama", 0) or 0),
            }
        )
    return observations


def merge_observations(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for observation in group:
            question_id = str(observation.get("question_id", ""))
            if not question_id:
                raise ValueError("every observation must have a question_id")
            destination = merged.setdefault(question_id, {"question_id": question_id})
            for key, value in observation.items():
                if key in destination and destination[key] != value:
                    raise ValueError(f"conflicting {key!r} values for question {question_id}")
                destination[key] = value
    return [merged[key] for key in sorted(merged)]


def attach_run_telemetry(observations: list[dict[str, Any]], run_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not observations or not run_state:
        return observations
    telemetry = run_state.get("telemetry") or {}
    first = observations[0]
    first["provider_calls"] = {
        "embedding": float(telemetry.get("embedding_calls", 0)),
        "write_llm": float(telemetry.get("llm_calls", 0)),
    }
    return observations


def compare_legacy_observations(
    baseline: list[dict[str, Any]], candidate_legacy: list[dict[str, Any]]
) -> dict[str, Any]:
    def index(values: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
        indexed = {}
        for observation in values:
            question_id = str(observation.get("question_id", ""))
            if not question_id or question_id in indexed:
                raise ValueError(f"{label} observations require unique non-empty question IDs")
            for field in ("candidate_fingerprints", "candidate_scores"):
                if not isinstance(observation.get(field), list):
                    raise ValueError(f"{label} observation {question_id} is missing {field}")
            if len(observation["candidate_fingerprints"]) != len(observation["candidate_scores"]):
                raise ValueError(f"{label} observation {question_id} has misaligned candidate data")
            for score in observation["candidate_scores"]:
                if (
                    not isinstance(score, dict)
                    or not score
                    or not set(score).issubset({"score", "rerank_score"})
                    or not all(
                        isinstance(value, (int, float)) and not isinstance(value, bool) for value in score.values()
                    )
                ):
                    raise ValueError(f"{label} observation {question_id} has invalid candidate_scores")
            indexed[question_id] = observation
        return indexed

    baseline_by_id = index(baseline, "baseline")
    legacy_by_id = index(candidate_legacy, "candidate_legacy")
    if not baseline_by_id and not legacy_by_id:
        return {
            "schema_version": 1,
            "passed": False,
            "sample_count": 0,
            "mismatches": [{"question_id": "<none>", "fields": ["no_observations"]}],
        }
    mismatches = []
    for question_id in sorted(set(baseline_by_id) | set(legacy_by_id)):
        if question_id not in baseline_by_id or question_id not in legacy_by_id:
            mismatches.append({"question_id": question_id, "fields": ["question_presence"]})
            continue
        fields = [
            field
            for field in ("candidate_fingerprints", "candidate_scores")
            if baseline_by_id[question_id][field] != legacy_by_id[question_id][field]
        ]
        if fields:
            mismatches.append({"question_id": question_id, "fields": fields})
    return {
        "schema_version": 1,
        "passed": not mismatches,
        "sample_count": len(baseline_by_id),
        "mismatches": mismatches,
    }
