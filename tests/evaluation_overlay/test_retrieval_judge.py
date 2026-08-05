from evaluation_overlay.metrics import summarize_observations
from evaluation_overlay.retrieval_judge import _load_run_binding, judge_retrieval_candidates


def test_generic_retrieval_judge_reports_grounded_claim_recall_at_each_cutoff_without_content():
    result = {
        "metadata": {"top_k_cutoffs": ["top_1", "top_2"]},
        "evaluations": [
            {
                "question_id": "q1",
                "question": "where?",
                "ground_truth_answer": "Will moved to Tokyo in 2024.",
                "retrieval": {
                    "search_results": [
                        {"id": "m1", "memory": "unrelated"},
                        {"id": "m2", "memory": "Will moved to Tokyo."},
                        {"id": "m3", "memory": "The move happened during 2024."},
                    ]
                },
            }
        ],
    }

    observations = judge_retrieval_candidates(
        result,
        lambda system, prompt: {
            "claims": [
                {
                    "reference_excerpt": "Will moved to Tokyo",
                    "candidate_index": 2,
                    "evidence": "Will moved to Tokyo",
                },
                {"reference_excerpt": "in 2024", "candidate_index": None, "evidence": None},
            ]
        },
    )
    summary = summarize_observations(observations, cutoffs=(1, 2))

    assert observations == [
        {
            "question_id": "q1",
            "candidate_evidence_coverage_scores": {"1": 0.0, "2": 4 / 6},
        }
    ]
    assert summary["candidate_recall_at_k"] == {"1": None, "2": None}
    assert summary["candidate_evidence_coverage_at_k"] == {"1": 0.0, "2": 4 / 6}


def test_generic_retrieval_judge_rejects_ungrounded_or_out_of_range_evidence():
    result = {
        "metadata": {"top_k_cutoffs": [1]},
        "evaluations": [
            {
                "question_id": "q1",
                "question": "where?",
                "ground_truth_answer": "Tokyo",
                "retrieval": {"search_results": [{"memory": "Will lives in Kyoto."}]},
            }
        ],
    }

    for assessment in (
        {"claims": [{"reference_excerpt": "Tokyo", "candidate_index": 1, "evidence": "Tokyo"}]},
        {"claims": [{"reference_excerpt": "Tokyo", "candidate_index": 2, "evidence": "Kyoto"}]},
        {"claims": []},
    ):
        try:
            judge_retrieval_candidates(result, lambda system, prompt, value=assessment: value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"assessment should fail closed: {assessment}")


def test_generic_retrieval_judge_rejects_trivial_claim_that_omits_reference_facts():
    result = {
        "metadata": {"top_k_cutoffs": [1]},
        "evaluations": [
            {
                "question_id": "q1",
                "question": "where and when?",
                "ground_truth_answer": "Will moved to Tokyo in 2024.",
                "retrieval": {"search_results": [{"memory": "Will"}]},
            }
        ],
    }
    assessment = {"claims": [{"reference_excerpt": "Will", "candidate_index": 1, "evidence": "Will"}]}

    try:
        judge_retrieval_candidates(result, lambda system, prompt: assessment)
    except ValueError as exc:
        assert "cover every reference token" in str(exc)
    else:
        raise AssertionError("trivial claim decomposition should fail closed")


def test_generic_retrieval_judge_rejects_excerpt_that_does_not_extractively_support_claim():
    result = {
        "metadata": {"top_k_cutoffs": [1]},
        "evaluations": [
            {
                "question_id": "q1",
                "question": "where and when?",
                "ground_truth_answer": "Will moved to Tokyo in 2024.",
                "retrieval": {"search_results": [{"memory": "Tokyo"}]},
            }
        ],
    }
    assessment = {
        "claims": [
            {
                "reference_excerpt": "Will moved to Tokyo in 2024",
                "candidate_index": 1,
                "evidence": "Tokyo",
            }
        ]
    }

    try:
        judge_retrieval_candidates(result, lambda system, prompt: assessment)
    except ValueError as exc:
        assert "does not contain the complete reference excerpt" in str(exc)
    else:
        raise AssertionError("partial evidence should not receive extractive coverage credit")


def test_generic_retrieval_judge_rejects_non_earliest_supporting_candidate():
    result = {
        "metadata": {"top_k_cutoffs": [2]},
        "evaluations": [
            {
                "question_id": "q1",
                "question": "where?",
                "ground_truth_answer": "Tokyo",
                "retrieval": {"search_results": [{"memory": "Tokyo"}, {"memory": "Tokyo"}]},
            }
        ],
    }
    assessment = {"claims": [{"reference_excerpt": "Tokyo", "candidate_index": 2, "evidence": "Tokyo"}]}

    try:
        judge_retrieval_candidates(result, lambda system, prompt: assessment)
    except ValueError as exc:
        assert "earliest candidate" in str(exc)
    else:
        raise AssertionError("non-earliest evidence rank should fail closed")


def test_judge_run_binding_requires_completed_run_and_input_below_its_output(tmp_path):
    run_dir = tmp_path / "runs" / "candidate" / "locomo"
    input_path = run_dir / "output" / "result.json"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("{}", encoding="utf-8")
    state_path = run_dir / "run-state.json"
    state_path.write_text(
        '{"status":"completed","fingerprint":"' + "a" * 64 + '","arm":"candidate","benchmark":"locomo"}',
        encoding="utf-8",
    )

    binding = _load_run_binding(input_path, state_path)

    assert binding["fingerprint"] == "a" * 64
    outside = tmp_path / "unbound.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        _load_run_binding(outside, state_path)
    except ValueError as exc:
        assert "run output directory" in str(exc)
    else:
        raise AssertionError("unbound judge input should fail closed")


def test_duplicate_supported_claims_cannot_inflate_reference_token_coverage():
    result = {
        "metadata": {"top_k_cutoffs": [1]},
        "evaluations": [
            {
                "question_id": "q1",
                "question": "where and when?",
                "ground_truth_answer": "Tokyo in 2024",
                "retrieval": {"search_results": [{"memory": "Tokyo"}]},
            }
        ],
    }
    assessment = {
        "claims": [
            *[{"reference_excerpt": "Tokyo", "candidate_index": 1, "evidence": "Tokyo"} for _ in range(100)],
            {"reference_excerpt": "in 2024", "candidate_index": None, "evidence": None},
        ]
    }

    observations = judge_retrieval_candidates(result, lambda system, prompt: assessment)

    assert observations[0]["candidate_evidence_coverage_scores"]["1"] == 1 / 3
