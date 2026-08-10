"""Tests for the scorecard diff tool (Phase A instrumentation)."""
from __future__ import annotations


def _card(arm: str, f1: float):
    from evaluation_instrument.scorecard import Scorecard

    return Scorecard(
        benchmark="locomo",
        arm=arm,
        project_name="p",
        mem0_host="h",
        model="m",
        scores={"f1": f1},
        question_count=40,
    )


def test_diff_reports_delta_no_regression():
    from evaluation_instrument.scorecard_diff import diff

    report = diff(_card("vanilla", 0.5), _card("candidate", 0.6))
    assert report["scores"]["f1"]["delta"] == 0.1
    assert report["regressed"] is False


def test_diff_flags_regression():
    from evaluation_instrument.scorecard_diff import diff

    report = diff(_card("vanilla", 0.6), _card("candidate", 0.5))
    assert report["scores"]["f1"]["delta"] == -0.1
    assert report["regressed"] is True


def test_diff_missing_candidate_metric_is_not_regression():
    from evaluation_instrument.scorecard_diff import diff

    report = diff(_card("vanilla", 0.5), _card("candidate", 0.5))
    assert report["benchmark"] == "locomo"
    assert report["scores"]["f1"]["candidate"] == 0.5
