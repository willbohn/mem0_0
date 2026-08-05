import subprocess
from pathlib import Path

import pytest

from evaluation_overlay.harness import load_manifest
from evaluation_overlay.server import _load_arm_config, _repository_revision, _search_arguments


def test_arm_config_expands_local_endpoints_and_isolates_collection_and_history(tmp_path, monkeypatch):
    manifest_path = Path("evaluation_overlay/manifest.json").resolve()
    manifest = load_manifest(manifest_path)
    monkeypatch.setenv("MEM0_BENCH_LOCAL_API_KEY", "local-test-value")
    monkeypatch.setenv("MEM0_BENCH_LLM_BASE_URL", "http://127.0.0.1:18000/v1")
    monkeypatch.setenv("MEM0_BENCH_EMBED_BASE_URL", "http://127.0.0.1:18001/v1")

    candidate = _load_arm_config(manifest_path, manifest, "candidate", tmp_path)
    legacy = _load_arm_config(manifest_path, manifest, "candidate_legacy", tmp_path)

    assert candidate["recall"] == {"mode": "state_aware"}
    assert legacy["recall"] == {"mode": "legacy"}
    assert candidate["llm"]["config"]["openai_base_url"] == "http://127.0.0.1:18000/v1"
    assert candidate["embedder"]["config"]["openai_base_url"] == "http://127.0.0.1:18001/v1"
    assert candidate["vector_store"]["config"]["collection_name"] != legacy["vector_store"]["config"]["collection_name"]
    assert candidate["history_db_path"] != legacy["history_db_path"]


def test_benchmark_rest_search_translates_legacy_payload_to_current_memory_api():
    filters, top_k = _search_arguments({"query": "q", "user_id": "u1", "limit": 200})

    assert filters == {"user_id": "u1"}
    assert top_k == 200


def test_repository_attestation_rejects_dirty_worktree(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout=" M mem0/memory/main.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="not clean"):
        _repository_revision(tmp_path)
