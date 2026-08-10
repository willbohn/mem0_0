"""Tests for the benchmark arm-runner command builder (Phase A instrumentation)."""
from __future__ import annotations

import pytest


def test_arm_command_construction_vanilla():
    from evaluation_instrument.run_arm import build_command

    cmd = build_command(
        benchmark="locomo",
        arm="vanilla",
        project_name="p",
        mem0_host="http://localhost:8888",
        model="m",
    )
    joined = " ".join(cmd)
    assert "--backend" in cmd and "oss" in cmd
    assert "--mem0-host" in cmd and "http://localhost:8888" in cmd
    assert any("benchmarks.locomo.run" in part for part in cmd)
    # arm is namespaced into the project name so the two arms never collide
    assert "p-vanilla" in joined


def test_arm_command_candidate_host():
    from evaluation_instrument.run_arm import build_command

    cmd = build_command(
        benchmark="beam",
        arm="candidate",
        project_name="sa12",
        mem0_host="http://127.0.0.1:28889",
        model="m",
    )
    assert "http://127.0.0.1:28889" in cmd
    assert "sa12-candidate" in " ".join(cmd)


def test_unknown_benchmark_rejected():
    from evaluation_instrument.run_arm import build_command

    with pytest.raises(ValueError, match="unknown benchmark"):
        build_command(
            benchmark="nope", arm="vanilla", project_name="p",
            mem0_host="h", model="m",
        )


def test_unknown_arm_rejected():
    from evaluation_instrument.run_arm import build_command

    with pytest.raises(ValueError, match="unknown arm"):
        build_command(
            benchmark="locomo", arm="third", project_name="p",
            mem0_host="h", model="m",
        )
