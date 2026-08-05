import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation_overlay.harness import load_manifest
from evaluation_overlay.server import (
    _apply_llm_runtime_budgets,
    _installed_runtime_packages,
    _load_arm_config,
    _prepare_runtime_directories,
    _repository_revision,
    _runtime_requirements_sha256,
    _search_arguments,
)


def test_llm_runtime_budgets_disable_hidden_provider_retries():
    memory = SimpleNamespace(llm=SimpleNamespace(client=SimpleNamespace(max_retries=2)))

    _apply_llm_runtime_budgets(memory, {"budgets": {"provider_max_retries": 0}})

    assert memory.llm.client.max_retries == 0


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
    assert candidate["llm"]["config"]["max_tokens"] == manifest["budgets"]["write_max_tokens"]
    assert candidate["embedder"]["config"]["openai_base_url"] == "http://127.0.0.1:18001/v1"
    assert candidate["vector_store"]["config"]["collection_name"] != legacy["vector_store"]["config"]["collection_name"]
    assert candidate["history_db_path"] != legacy["history_db_path"]


def test_prepare_runtime_directories_creates_history_parent(tmp_path):
    history_path = tmp_path / "nested" / ".mem0_0" / "history.db"

    _prepare_runtime_directories({"history_db_path": str(history_path)})

    assert history_path.parent.is_dir()
    assert not history_path.exists()


def test_runtime_requirements_fingerprint_is_content_bound(tmp_path):
    (tmp_path / "runtime-requirements.txt").write_text("fastapi==1\n", encoding="utf-8")

    assert _runtime_requirements_sha256(tmp_path / "manifest.json") == hashlib.sha256(b"fastapi==1\n").hexdigest()


def test_installed_runtime_packages_attests_every_pinned_distribution(monkeypatch):
    versions = {"fastapi": "1.2.3", "starlette": "4.5.6"}
    monkeypatch.setattr("evaluation_overlay.server.importlib.metadata.version", versions.__getitem__)

    assert _installed_runtime_packages({"fastapi": "expected", "starlette": "expected"}) == versions


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
