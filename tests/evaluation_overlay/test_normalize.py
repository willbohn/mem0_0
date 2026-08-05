import json

from evaluation_overlay.metrics import summarize_observations
from evaluation_overlay.normalize import (
    attach_run_telemetry,
    compare_legacy_observations,
    merge_observations,
    normalize_memora_judge,
    normalize_stale_judge,
    normalize_upstream_result,
)


def test_upstream_normalizer_strips_content_and_preserves_scores_cost_and_size():
    result = {
        "metadata": {"top_k_cutoffs": ["top_10"]},
        "evaluations": [
            {
                "question_id": "q1",
                "retrieval": {
                    "search_latency_ms": 12,
                    "search_results": [{"id": "m1", "memory": "private content", "score": 0.9}],
                },
                "cutoff_results": {"top_10": {"score": 0.5, "generated_answer": "private answer"}},
            }
        ],
    }
    state = {
        "telemetry": {
            "add_requests": 2,
            "embedding_calls": 4,
            "llm_calls": 2,
            "write_latency_ms": 20,
        }
    }

    observations = normalize_upstream_result(result, run_state=state)

    assert observations == [
        {
            "question_id": "q1",
            "candidate_ids": ["m1"],
            "candidate_fingerprints": ["1eb1e277f99fe7cc799941c72fa82ffbfcd7ce4e7c795ca1d02172c3ad4bbd73"],
            "candidate_scores": [{"score": 0.9}],
            "answer_score": 0.5,
            "retrieved_chars": 15,
            "retrieved_tokens": 2,
            "search_latency_ms": 12.0,
            "write_latency_ms": 10.0,
            "provider_calls": {"embedding": 4.0, "write_llm": 2.0},
        }
    ]
    serialized = json.dumps(observations)
    assert "private content" not in serialized
    assert "private answer" not in serialized


def test_official_stale_and_memora_judge_outputs_normalize_without_question_specific_rules():
    stale = normalize_stale_judge(
        {
            "details": [
                {
                    "uid": "s1",
                    "evaluation": {
                        "dim1_eval": {"pass": True},
                        "dim2_eval": {"pass": False},
                        "dim3_eval": {"pass": True},
                    },
                }
            ]
        }
    )
    memora = normalize_memora_judge(
        {
            "results": [
                {
                    "question_id": "m1",
                    "total_eval_questions": 4,
                    "memory_presence_correct": 2,
                    "forgetting_absence_correct": 1,
                    "fama": 0.625,
                }
            ]
        }
    )

    stale_summary = summarize_observations(stale)
    memora_summary = summarize_observations(memora)
    assert stale_summary["state_resolution_accuracy"] == 2 / 3
    assert memora_summary["answer_accuracy"] == 0.75
    assert memora_summary["forgetting_aware_memory_accuracy"] == 0.625


def test_retrieval_and_judge_observations_merge_without_content():
    merged = merge_observations(
        [
            [{"question_id": "q1", "candidate_ids": ["m1"], "retrieved_chars": 10}],
            [{"question_id": "q1", "answer_score": 1.0, "state_resolution_correct": True}],
        ]
    )

    assert merged == [
        {
            "question_id": "q1",
            "candidate_ids": ["m1"],
            "retrieved_chars": 10,
            "answer_score": 1.0,
            "state_resolution_correct": True,
        }
    ]


def test_official_judge_observations_receive_attested_provider_telemetry():
    observations = attach_run_telemetry(
        [{"question_id": "q1", "answer_score": 1.0}],
        {"telemetry": {"embedding_calls": 3, "llm_calls": 1, "write_latency_samples_ms": [4.0, 6.0]}},
    )

    assert observations[0]["provider_calls"] == {"embedding": 3.0, "write_llm": 1.0}
    assert "write_latency_samples_ms" not in observations[0]


def test_legacy_comparison_enforces_exact_content_order_and_final_scores():
    baseline = [
        {
            "question_id": "q1",
            "candidate_fingerprints": ["content-a", "content-b"],
            "candidate_scores": [{"score": 0.9}, {"score": 0.7}],
        }
    ]
    matching = [
        {
            "question_id": "q1",
            "candidate_fingerprints": ["content-a", "content-b"],
            "candidate_scores": [{"score": 0.9}, {"score": 0.7}],
        }
    ]
    reordered = [
        {
            "question_id": "q1",
            "candidate_fingerprints": ["content-b", "content-a"],
            "candidate_scores": [{"score": 0.9}, {"score": 0.7}],
        }
    ]

    assert compare_legacy_observations(baseline, matching)["passed"] is True
    mismatch = compare_legacy_observations(baseline, reordered)
    assert mismatch["passed"] is False
    assert mismatch["mismatches"] == [{"question_id": "q1", "fields": ["candidate_fingerprints"]}]


def test_legacy_comparison_fails_closed_on_missing_query_or_score():
    baseline = [
        {
            "question_id": "q1",
            "candidate_fingerprints": ["a"],
            "candidate_scores": [{"score": 0.5}],
        }
    ]

    missing = compare_legacy_observations(baseline, [])
    assert missing["passed"] is False
    try:
        compare_legacy_observations(baseline, [{"question_id": "q1", "candidate_fingerprints": ["a"]}])
    except ValueError as exc:
        assert "candidate_scores" in str(exc)
    else:
        raise AssertionError("missing score data should fail closed")


def test_legacy_comparison_and_normalization_fail_closed_on_no_observations():
    comparison = compare_legacy_observations([], [])

    assert comparison["passed"] is False
    assert comparison["sample_count"] == 0
    try:
        normalize_upstream_result(
            {
                "metadata": {"top_k_cutoffs": ["top_10"]},
                "evaluations": [{"question_id": "q1", "cutoff_results": {}}],
            }
        )
    except ValueError as exc:
        assert "no evaluations" in str(exc)
    else:
        raise AssertionError("missing cutoff observations should fail closed")
