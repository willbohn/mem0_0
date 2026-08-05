import hashlib
import json
import subprocess

import pytest

from evaluation_overlay.harness import (
    CheckpointMismatchError,
    ManifestError,
    build_run_spec,
    expected_runtime_attestation,
    load_manifest,
    run_benchmark,
    verify_dataset_receipts,
    verify_model_receipts,
    verify_model_endpoints,
    verify_sources,
)


def _manifest(tmp_path):
    revision = "a" * 40
    manifest = {
        "schema_version": 1,
        "repository": {"baseline_revision": "b" * 40, "candidate_revision": "c" * 40},
        "sources": {
            "memory_benchmarks": {
                "kind": "git",
                "location": "evaluation",
                "repository": "https://example.invalid/memory-benchmarks.git",
                "revision": revision,
            }
        },
        "datasets": {
            name: {
                "kind": "huggingface",
                "repository": f"example/{name}",
                "revision": "d" * 40,
            }
            for name in ("locomo", "longmemeval", "beam", "stale", "memora")
        },
        "models": {
            "llm": {"id": "example/model", "revision": "e" * 40},
            "embedder": {"id": "example/embedder", "revision": "f" * 40},
        },
        "arms": {
            name: {
                "mem0_host": f"http://127.0.0.1:{port}",
                "store_id": name,
                "config": f"config/{name}.json",
                "recall_mode": "state_aware" if name == "candidate" else "legacy",
            }
            for name, port in (("baseline", 18888), ("candidate", 28888), ("candidate_legacy", 38888))
        },
        "benchmarks": {
            name: {
                "source": "memory_benchmarks",
                "dataset": name,
                "command": ["{python}", "-c", "print('ok')", "{output_dir}", "{dataset_dir}"],
            }
            for name in ("locomo", "longmemeval", "beam", "stale", "memora")
        },
        "budgets": {"seed": 42, "top_k": 200, "top_k_cutoffs": [10, 20, 50, 200], "max_workers": 1},
        "gates": {"answer_accuracy": {"delta_min": -0.01}},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_manifest_requires_all_arms_and_benchmarks(tmp_path):
    path = _manifest(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["benchmarks"]["stale"]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ManifestError, match="stale"):
        load_manifest(path)


def test_run_specs_isolate_host_store_and_runtime_directories(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))

    specs = [build_run_spec(manifest, tmp_path / "work", side, "locomo") for side in manifest["arms"]]

    assert len({spec.environment["MEM0_HOST"] for spec in specs}) == 3
    assert len({spec.environment["MEM0_BENCH_STORE_ID"] for spec in specs}) == 3
    assert len({spec.environment["MEM0_DIR"] for spec in specs}) == 3
    assert len({spec.fingerprint for spec in specs}) == 3
    assert all(str(tmp_path / "work") in str(spec.output_dir) for spec in specs)


def test_checkpoint_resume_rejects_changed_manifest(tmp_path, monkeypatch):
    path = _manifest(tmp_path)
    manifest = load_manifest(path)
    work_dir = tmp_path / "work"
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "evaluation_overlay.harness.verify_runtime_attestation",
        lambda manifest, arm: {"telemetry": {}},
    )
    first = build_run_spec(manifest, work_dir, "candidate", "locomo")
    run_benchmark(first)

    value = json.loads(path.read_text(encoding="utf-8"))
    value["budgets"]["top_k"] = 50
    path.write_text(json.dumps(value), encoding="utf-8")
    changed = build_run_spec(load_manifest(path), work_dir, "candidate", "locomo")

    with pytest.raises(CheckpointMismatchError):
        run_benchmark(changed, resume=True)
    assert len(calls) == 1


def test_completed_exact_resume_is_skipped(tmp_path, monkeypatch):
    manifest = load_manifest(_manifest(tmp_path))
    spec = build_run_spec(manifest, tmp_path / "work", "candidate", "locomo")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "evaluation_overlay.harness.verify_runtime_attestation",
        lambda manifest, arm: {"telemetry": {}},
    )
    assert run_benchmark(spec).status == "completed"
    assert run_benchmark(spec, resume=True).status == "skipped"
    assert len(calls) == 1


def test_verify_sources_detects_wrong_git_revision(tmp_path, monkeypatch):
    manifest = load_manifest(_manifest(tmp_path))
    (tmp_path / "evaluation").mkdir()

    def fake_run(*args, **kwargs):
        command = args[0]
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="0" * 40 + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestError, match="expected"):
        verify_sources(manifest, tmp_path)


def test_dataset_receipts_verify_consumed_file_hashes(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    for name, dataset in manifest["datasets"].items():
        dataset_dir = tmp_path / "work" / "datasets" / name
        dataset_dir.mkdir(parents=True)
        payload = f"{name}\n".encode()
        (dataset_dir / "data.json").write_bytes(payload)
        receipt = {key: dataset[key] for key in ("kind", "repository", "revision")}
        receipt["files"] = {"data.json": hashlib.sha256(payload).hexdigest()}
        (dataset_dir / ".source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    verified = verify_dataset_receipts(manifest, tmp_path / "work")

    assert set(verified) == {"locomo", "longmemeval", "beam", "stale", "memora"}


def test_dataset_receipts_fail_closed_on_content_drift(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    dataset = manifest["datasets"]["locomo"]
    dataset_dir = tmp_path / "work" / "datasets" / "locomo"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "data.json").write_text("changed", encoding="utf-8")
    receipt = {key: dataset[key] for key in ("kind", "repository", "revision")}
    receipt["files"] = {"data.json": "0" * 64}
    (dataset_dir / ".source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ManifestError, match="SHA-256 verification"):
        verify_dataset_receipts(manifest, tmp_path / "work")


def test_model_receipts_verify_artifact_hashes(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    for name, model in manifest["models"].items():
        model_dir = tmp_path / "work" / "models" / name
        model_dir.mkdir(parents=True)
        payload = f"{name}\n".encode()
        (model_dir / "artifact.bin").write_bytes(payload)
        receipt = {key: model[key] for key in ("id", "revision")}
        receipt["files"] = {"artifact.bin": hashlib.sha256(payload).hexdigest()}
        (model_dir / ".source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    verified = verify_model_receipts(manifest, tmp_path / "work")

    assert set(verified) == {"llm", "embedder"}
    assert all(value["receipt_sha256"] for value in verified.values())


def test_runtime_attestation_binds_each_arm_to_revision_mode_models_and_store(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))

    baseline = expected_runtime_attestation(manifest, "baseline")
    candidate = expected_runtime_attestation(manifest, "candidate")
    legacy = expected_runtime_attestation(manifest, "candidate_legacy")

    assert baseline["repository_revision"] == manifest["repository"]["baseline_revision"]
    assert candidate["repository_revision"] == legacy["repository_revision"]
    assert candidate["recall_mode"] == "state_aware"
    assert baseline["recall_mode"] == legacy["recall_mode"] == "legacy"
    assert len({baseline["store_id"], candidate["store_id"], legacy["store_id"]}) == 3


def test_model_endpoint_verification_requires_both_pinned_ids(tmp_path, monkeypatch):
    manifest = load_manifest(_manifest(tmp_path))
    receipts = {
        "llm": {"receipt_sha256": "1" * 64},
        "embedder": {"receipt_sha256": "2" * 64},
    }
    monkeypatch.setenv("MEM0_BENCH_LOCAL_API_KEY", "local")
    monkeypatch.setenv("MEM0_BENCH_LLM_BASE_URL", "http://127.0.0.1:18000/v1")
    monkeypatch.setenv("MEM0_BENCH_EMBED_BASE_URL", "http://127.0.0.1:18001/v1")

    class Response:
        def __init__(self, model_id, receipt_sha256):
            self.model_id = model_id
            self.receipt_sha256 = receipt_sha256

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {
                    "data": [
                        {
                            "id": self.model_id,
                            "metadata": {"mem0_0_artifact_fingerprint": self.receipt_sha256},
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        name = "llm" if "18000" in request.full_url else "embedder"
        return Response(manifest["models"][name]["id"], receipts[name]["receipt_sha256"])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert verify_model_endpoints(manifest, receipts) == {
        "llm": {
            "base_url": "http://127.0.0.1:18000/v1",
            "model_id": manifest["models"]["llm"]["id"],
            "receipt_sha256": receipts["llm"]["receipt_sha256"],
        },
        "embedder": {
            "base_url": "http://127.0.0.1:18001/v1",
            "model_id": manifest["models"]["embedder"]["id"],
            "receipt_sha256": receipts["embedder"]["receipt_sha256"],
        },
    }


def test_model_endpoint_verification_rejects_unbound_advertised_model(tmp_path, monkeypatch):
    manifest = load_manifest(_manifest(tmp_path))
    receipts = {
        "llm": {"receipt_sha256": "1" * 64},
        "embedder": {"receipt_sha256": "2" * 64},
    }
    monkeypatch.setenv("MEM0_BENCH_LOCAL_API_KEY", "local")
    monkeypatch.setenv("MEM0_BENCH_LLM_BASE_URL", "http://127.0.0.1:18000/v1")
    monkeypatch.setenv("MEM0_BENCH_EMBED_BASE_URL", "http://127.0.0.1:18001/v1")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {
                    "data": [
                        {
                            "id": manifest["models"]["llm"]["id"],
                            "metadata": {"mem0_0_artifact_fingerprint": "9" * 64},
                        }
                    ]
                }
            ).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())

    with pytest.raises(ManifestError, match="artifact fingerprint"):
        verify_model_endpoints(manifest, receipts)
