"""Run one benchmark arm (vanilla or candidate) and emit a content-free scorecard.

The arm switch is `--mem0-host`: vanilla points at the legacy server
(http://localhost:8888), candidate at the canonical shadow
(http://127.0.0.1:28889). Both arms use `--backend oss`. The benchmark runners
live in the `evaluation/` git submodule and are invoked with `evaluation/` on
the subprocess `PYTHONPATH` (they are not importable from the repo root).
"""
from __future__ import annotations

from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parent.parent / "evaluation"

_RUNNERS = {
    "locomo": "benchmarks.locomo.run",
    "longmemeval": "benchmarks.longmemeval.run",
    "beam": "benchmarks.beam.run",
}

_ARMS = ("vanilla", "candidate")


def build_command(*, benchmark: str, arm: str, project_name: str, mem0_host: str, model: str) -> list[str]:
    if benchmark not in _RUNNERS:
        raise ValueError(f"unknown benchmark: {benchmark}")
    if arm not in _ARMS:
        raise ValueError(f"unknown arm: {arm}")
    return [
        "python",
        "-m",
        _RUNNERS[benchmark],
        "--backend",
        "oss",
        "--mem0-host",
        mem0_host,
        "--project-name",
        f"{project_name}-{arm}",
    ]
