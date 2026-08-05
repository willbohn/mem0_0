import errno
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from evaluation_overlay.harness import (
    CheckpointMismatchError,
    ManifestError,
    build_run_spec,
    derive_dataset_receipt,
    expected_runtime_attestation,
    load_manifest,
    materialize_model_artifact,
    prepare_dataset_receipt,
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
                "required_files": ["locomo10.json"] if name == "locomo" else ["data.json"],
                "revision": "d" * 40,
            }
            for name in ("locomo", "longmemeval", "beam", "stale", "memora")
        },
        "models": {
            "llm": {
                "id": "example/model",
                "revision": "e" * 40,
                "artifact": {
                    "format": "gguf",
                    "path": "model.gguf",
                    "repository": "example/model-gguf",
                    "revision": "1" * 40,
                    "sha256": hashlib.sha256(b"model\n").hexdigest(),
                    "size": 6,
                },
            },
            "embedder": {
                "id": "example/embedder",
                "revision": "f" * 40,
                "artifact": {
                    "format": "gguf",
                    "path": "embedder.gguf",
                    "repository": "example/embedder-gguf",
                    "revision": "3" * 40,
                    "sha256": hashlib.sha256(b"embedder\n").hexdigest(),
                    "size": 9,
                },
            },
        },
        "runtimes": {
            "benchmark_server": {
                "fastapi": "0.141.1",
                "packages": {"fastapi": "0.141.1", "uvicorn": "0.52.0"},
                "requirements_sha256": "8" * 64,
                "uvicorn": "0.52.0",
            },
            "llama_server": {
                "binary": "llama-server",
                "device": "Vulkan0",
                "enable_thinking": False,
                "gpu_layers": "all",
                "sha256": "7" * 64,
                "version": "10216",
            },
            "qdrant": {
                "container_name": "mem0_0-benchmark-qdrant",
                "host": "127.0.0.1",
                "image": "docker.io/qdrant/qdrant@sha256:" + "5" * 64,
                "platform": "linux/amd64",
                "platform_digest": "sha256:" + "6" * 64,
                "port": 6333,
                "version": "1.18.3",
            },
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


def test_manifest_requires_digest_pinned_qdrant_runtime(tmp_path):
    path = _manifest(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["runtimes"]["qdrant"]["image"] = "docker.io/qdrant/qdrant:latest"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ManifestError, match="digest-pinned"):
        load_manifest(path)


def test_manifest_requires_pinned_llama_server_binary(tmp_path):
    path = _manifest(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["runtimes"]["llama_server"]["sha256"]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ManifestError, match="runtimes.llama_server.sha256"):
        load_manifest(path)


def test_manifest_requires_exact_model_artifact_pin(tmp_path):
    path = _manifest(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["models"]["embedder"]["artifact"]["sha256"]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ManifestError, match="models.embedder.artifact.sha256"):
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
        relative_path = dataset["required_files"][0]
        (dataset_dir / relative_path).write_bytes(payload)
        receipt = {key: dataset[key] for key in ("kind", "repository", "required_files", "revision")}
        receipt["files"] = {relative_path: hashlib.sha256(payload).hexdigest()}
        (dataset_dir / ".source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    verified = verify_dataset_receipts(manifest, tmp_path / "work")

    assert set(verified) == {"locomo", "longmemeval", "beam", "stale", "memora"}


def test_dataset_receipts_fail_closed_on_content_drift(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    dataset = manifest["datasets"]["locomo"]
    dataset_dir = tmp_path / "work" / "datasets" / "locomo"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "locomo10.json").write_text("changed", encoding="utf-8")
    receipt = {key: dataset[key] for key in ("kind", "repository", "required_files", "revision")}
    receipt["files"] = {"locomo10.json": "0" * 64}
    (dataset_dir / ".source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ManifestError, match="SHA-256 verification"):
        verify_dataset_receipts(manifest, tmp_path / "work")


def test_prepare_dataset_receipt_hashes_exact_manifest_required_files(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    dataset_dir = tmp_path / "work" / "datasets" / "locomo"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "locomo10.json").write_bytes(b"pinned\n")

    receipt = prepare_dataset_receipt(
        manifest,
        tmp_path / "work",
        "locomo",
        ["locomo10.json"],
    )
    for other in ("longmemeval", "beam", "stale", "memora"):
        other_dir = tmp_path / "work" / "datasets" / other
        other_dir.mkdir(parents=True)
        (other_dir / "data.json").write_bytes(b"other\n")
        prepare_dataset_receipt(manifest, tmp_path / "work", other, ["data.json"])

    assert receipt["files"] == {"locomo10.json": hashlib.sha256(b"pinned\n").hexdigest()}
    assert verify_dataset_receipts(manifest, tmp_path / "work")["locomo"]["files"] == receipt["files"]


def test_prepare_dataset_receipt_rejects_a_subset_of_required_files(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    manifest["datasets"]["beam"]["required_files"] = ["source.parquet", "beam.json"]
    dataset_dir = tmp_path / "work" / "datasets" / "beam"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "source.parquet").write_bytes(b"source")

    with pytest.raises(ManifestError, match="exactly match required_files"):
        prepare_dataset_receipt(manifest, tmp_path / "work", "beam", ["source.parquet"])


def test_derive_dataset_receipt_executes_pinned_tool_and_receipts_inputs_and_outputs(tmp_path, monkeypatch):
    manifest = load_manifest(_manifest(tmp_path))
    binary = tmp_path / "duckdb"
    binary.write_bytes(b"binary")
    extension = tmp_path / "work" / "duckdb_extensions" / "json.duckdb_extension"
    extension.parent.mkdir(parents=True)
    extension.write_bytes(b"extension")
    dataset_dir = tmp_path / "work" / "datasets" / "beam"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "source.parquet").write_bytes(b"source")
    manifest["datasets"]["beam"].update(
        {
            "required_files": ["beam.json", "source.parquet"],
            "derivation": {
                "binary": "duckdb",
                "binary_sha256": hashlib.sha256(b"binary").hexdigest(),
                "extension_path": "duckdb_extensions/json.duckdb_extension",
                "json_extension_sha256": hashlib.sha256(b"extension").hexdigest(),
                "outputs": {"beam.json": ["source.parquet"]},
                "tool": "duckdb",
                "version": "duckdb-test",
            },
        }
    )
    monkeypatch.setattr("evaluation_overlay.harness.shutil.which", lambda _name: str(binary))

    def fake_run(command, **_kwargs):
        if command[-1] == "-version":
            return subprocess.CompletedProcess(command, 0, stdout="duckdb-test\n", stderr="")
        output_path = command[-1].split(" TO '", 1)[1].split("' (FORMAT", 1)[0]
        Path(output_path).write_bytes(b"[]\n")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("evaluation_overlay.harness.subprocess.run", fake_run)

    receipt = derive_dataset_receipt(manifest, tmp_path / "work", "beam")
    for other in ("locomo", "longmemeval", "stale", "memora"):
        relative_path = manifest["datasets"][other]["required_files"][0]
        other_dir = tmp_path / "work" / "datasets" / other
        other_dir.mkdir(parents=True)
        (other_dir / relative_path).write_bytes(b"other\n")
        prepare_dataset_receipt(manifest, tmp_path / "work", other, [relative_path])

    assert receipt["derivation_proof"]["kind"] == "mem0_0_executed_derivation_v1"
    assert set(receipt["files"]) == {"beam.json", "source.parquet"}
    assert verify_dataset_receipts(manifest, tmp_path / "work")["beam"]["files"] == receipt["files"]


def test_model_receipts_verify_artifact_hashes(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    for name, model in manifest["models"].items():
        model_dir = tmp_path / "work" / "models" / name
        model_dir.mkdir(parents=True)
        payload = b"model\n" if name == "llm" else b"embedder\n"
        (model_dir / model["artifact"]["path"]).write_bytes(payload)
        receipt = {key: model[key] for key in ("id", "revision")}
        receipt["artifact"] = model["artifact"]
        receipt["files"] = {model["artifact"]["path"]: hashlib.sha256(payload).hexdigest()}
        (model_dir / ".source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    verified = verify_model_receipts(manifest, tmp_path / "work")

    assert set(verified) == {"llm", "embedder"}
    assert all(value["receipt_sha256"] for value in verified.values())


def test_model_receipts_reject_unpinned_local_artifact(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    model = manifest["models"]["llm"]
    model_dir = tmp_path / "work" / "models" / "llm"
    model_dir.mkdir(parents=True)
    (model_dir / "different.gguf").write_bytes(b"model\n")
    receipt = {key: model[key] for key in ("id", "revision")}
    receipt["artifact"] = {**model["artifact"], "path": "different.gguf"}
    receipt["files"] = {"different.gguf": hashlib.sha256(b"model\n").hexdigest()}
    (model_dir / ".source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ManifestError, match="artifact pin"):
        verify_model_receipts(manifest, tmp_path / "work")


def test_materialize_model_artifact_creates_verified_hardlink_and_receipt(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    source = tmp_path / "source.gguf"
    source.write_bytes(b"model\n")
    embedder_source = tmp_path / "embedder.gguf"
    embedder_source.write_bytes(b"embedder\n")

    receipt = materialize_model_artifact(manifest, tmp_path / "work", "llm", source)
    materialize_model_artifact(manifest, tmp_path / "work", "embedder", embedder_source)

    target = tmp_path / "work" / "models" / "llm" / "model.gguf"
    assert target.read_bytes() == b"model\n"
    assert target.stat().st_ino == source.stat().st_ino
    assert receipt["artifact"] == manifest["models"]["llm"]["artifact"]
    assert verify_model_receipts(manifest, tmp_path / "work")["llm"]["files"] == {
        "model.gguf": hashlib.sha256(b"model\n").hexdigest()
    }


def test_materialize_model_artifact_rejects_wrong_bytes_without_writing(tmp_path):
    manifest = load_manifest(_manifest(tmp_path))
    source = tmp_path / "source.gguf"
    source.write_bytes(b"wrong")

    with pytest.raises(ManifestError, match="does not match the pinned artifact"):
        materialize_model_artifact(manifest, tmp_path / "work", "llm", source)

    assert not (tmp_path / "work" / "models" / "llm" / "model.gguf").exists()


def test_materialize_model_artifact_requires_explicit_cross_device_copy(tmp_path, monkeypatch):
    manifest = load_manifest(_manifest(tmp_path))
    source = tmp_path / "source.gguf"
    source.write_bytes(b"model\n")
    monkeypatch.setattr("os.link", lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))

    with pytest.raises(ManifestError, match="explicit copy approval"):
        materialize_model_artifact(manifest, tmp_path / "work", "llm", source)

    receipt = materialize_model_artifact(manifest, tmp_path / "work", "llm", source, allow_copy=True)

    target = tmp_path / "work" / "models" / "llm" / "model.gguf"
    assert target.read_bytes() == b"model\n"
    assert target.stat().st_ino != source.stat().st_ino
    assert receipt["files"] == {"model.gguf": hashlib.sha256(b"model\n").hexdigest()}


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
    assert baseline["qdrant_platform_digest"] == manifest["runtimes"]["qdrant"]["platform_digest"]
    assert baseline["fastapi_version"] == "0.141.1"
    assert baseline["runtime_requirements_sha256"] == "8" * 64
    assert baseline["runtime_packages"] == {"fastapi": "0.141.1", "uvicorn": "0.52.0"}
    assert baseline["uvicorn_version"] == "0.52.0"


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
            if self.model_id == "attestation":
                return json.dumps(
                    {
                        "artifact_sha256": self.receipt_sha256["artifact_sha256"],
                        "backend_verified": True,
                        "device": "Vulkan0",
                        "enable_thinking": False,
                        "gpu_layers": "all",
                        "kind": "mem0_0_model_proxy_v1",
                        "launcher_sha256": "7" * 64,
                        "model_id": self.receipt_sha256["model_id"],
                        "offloaded_layers": 25,
                        "receipt_sha256": self.receipt_sha256["receipt_sha256"],
                        "resolved_device": "Vulkan0",
                        "total_layers": 25,
                    }
                ).encode()
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
        if request.full_url.endswith("/mem0_0/attestation"):
            return Response(
                "attestation",
                {
                    "artifact_sha256": manifest["models"][name]["artifact"]["sha256"],
                    "model_id": manifest["models"][name]["id"],
                    "receipt_sha256": receipts[name]["receipt_sha256"],
                },
            )
        return Response(manifest["models"][name]["id"], receipts[name]["receipt_sha256"])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert verify_model_endpoints(manifest, receipts) == {
        "llm": {
            "base_url": "http://127.0.0.1:18000/v1",
            "model_id": manifest["models"]["llm"]["id"],
            "receipt_sha256": receipts["llm"]["receipt_sha256"],
            "proxy_attestation": "mem0_0_model_proxy_v1",
        },
        "embedder": {
            "base_url": "http://127.0.0.1:18001/v1",
            "model_id": manifest["models"]["embedder"]["id"],
            "receipt_sha256": receipts["embedder"]["receipt_sha256"],
            "proxy_attestation": "mem0_0_model_proxy_v1",
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


def test_model_endpoint_verification_rejects_non_loopback_endpoint(tmp_path, monkeypatch):
    manifest = load_manifest(_manifest(tmp_path))
    receipts = {
        "llm": {"receipt_sha256": "1" * 64},
        "embedder": {"receipt_sha256": "2" * 64},
    }
    monkeypatch.setenv("MEM0_BENCH_LOCAL_API_KEY", "local")
    monkeypatch.setenv("MEM0_BENCH_LLM_BASE_URL", "https://models.example.com/v1")
    monkeypatch.setenv("MEM0_BENCH_EMBED_BASE_URL", "http://127.0.0.1:18001/v1")

    with pytest.raises(ManifestError, match="loopback"):
        verify_model_endpoints(manifest, receipts)
