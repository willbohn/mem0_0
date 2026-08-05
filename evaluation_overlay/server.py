from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from evaluation_overlay.harness import expected_runtime_attestation, load_manifest
from evaluation_overlay.qdrant_runtime import verify_runtime as verify_qdrant_runtime


_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_expand_environment(child) for child in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise RuntimeError(f"required benchmark environment variable {name} is not set")
        return os.environ[name]

    return _ENV_REFERENCE.sub(replace, value)


def _load_arm_config(
    manifest_path: Path,
    manifest: dict[str, Any],
    arm: str,
    work_dir: Path,
    qdrant_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    qdrant_runtime = qdrant_runtime or manifest["runtimes"]["qdrant"]
    template_path = manifest_path.parent / manifest["arms"][arm]["config"]
    config = _expand_environment(json.loads(template_path.read_text(encoding="utf-8")))
    config["history_db_path"] = str(work_dir / "runtime" / arm / ".mem0_0" / "history.db")
    config["vector_store"] = {
        "provider": "qdrant",
        "config": {
            "collection_name": manifest["arms"][arm]["store_id"],
            "embedding_model_dims": config["embedder"]["config"]["embedding_dims"],
            "host": qdrant_runtime["host"],
            "port": qdrant_runtime["port"],
        },
    }
    return config


def _repository_revision(repository_root: Path) -> str:
    revision = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(repository_root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(f"benchmark repository worktree is not clean: {repository_root}")
    return revision


def _prepare_runtime_directories(config: dict[str, Any]) -> None:
    Path(config["history_db_path"]).parent.mkdir(parents=True, exist_ok=True)


def _runtime_requirements_sha256(manifest_path: Path) -> str:
    requirements = manifest_path.parent / "runtime-requirements.txt"
    return hashlib.sha256(requirements.read_bytes()).hexdigest()


def _installed_runtime_packages(packages: dict[str, str]) -> dict[str, str]:
    return {package: importlib.metadata.version(package) for package in packages}


def _search_arguments(request: dict[str, Any]) -> tuple[dict[str, Any], int]:
    filters = dict(request.get("filters") or {})
    for key in ("user_id", "agent_id", "run_id"):
        if request.get(key) is not None:
            filters[key] = request[key]
    return filters, int(request.get("top_k", request.get("limit", 100)))


class Telemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, float] = {
            "add_requests": 0,
            "embedding_calls": 0,
            "llm_calls": 0,
            "retrieved_chars": 0,
            "search_requests": 0,
            "write_latency_ms": 0,
            "search_latency_ms": 0,
        }
        self._samples: dict[str, list[float]] = {"search_latency_samples_ms": [], "write_latency_samples_ms": []}

    def add(self, key: str, value: float = 1) -> None:
        with self._lock:
            self._values[key] += value

    def sample(self, key: str, value: float) -> None:
        with self._lock:
            self._samples[key].append(value)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {**self._values, **{key: list(values) for key, values in self._samples.items()}}


def _instrument_provider_calls(memory: Any, telemetry: Telemetry) -> None:
    original_llm = memory.llm.generate_response

    def generate_response(*args: Any, **kwargs: Any) -> Any:
        telemetry.add("llm_calls")
        return original_llm(*args, **kwargs)

    memory.llm.generate_response = generate_response
    for method_name in ("embed", "embed_batch"):
        original = getattr(memory.embedding_model, method_name, None)
        if original is None:
            continue

        def instrumented(*args: Any, __original: Any = original, **kwargs: Any) -> Any:
            telemetry.add("embedding_calls")
            return __original(*args, **kwargs)

        setattr(memory.embedding_model, method_name, instrumented)


def _apply_llm_runtime_budgets(memory: Any, manifest: dict[str, Any]) -> None:
    """Apply manifest-pinned client behavior that older SDK revisions cannot configure directly."""
    memory.llm.client.max_retries = manifest["budgets"]["provider_max_retries"]


def create_app(
    manifest_path: Path,
    work_dir: Path,
    arm: str,
    repository_root: Path,
) -> Any:
    from fastapi import FastAPI, HTTPException, Query

    manifest = load_manifest(manifest_path)
    expected = expected_runtime_attestation(manifest, arm)
    qdrant_runtime = verify_qdrant_runtime(manifest, work_dir)
    actual_revision = _repository_revision(repository_root)
    if actual_revision != expected["repository_revision"]:
        raise RuntimeError(
            f"{arm} server expected repository {expected['repository_revision']} but loaded {actual_revision}"
        )

    sys.path.insert(0, str(repository_root))
    from mem0 import Memory

    os.environ["OPENAI_MAX_RETRIES"] = str(manifest["budgets"]["provider_max_retries"])
    config = _load_arm_config(manifest_path, manifest, arm, work_dir, qdrant_runtime)
    _prepare_runtime_directories(config)
    memory = Memory.from_config(config)
    _apply_llm_runtime_budgets(memory, manifest)
    loaded_module = Path(sys.modules["mem0"].__file__).resolve()
    if repository_root.resolve() not in loaded_module.parents:
        raise RuntimeError(f"mem0 loaded from {loaded_module}, not pinned worktree {repository_root}")

    telemetry = Telemetry()
    _instrument_provider_calls(memory, telemetry)
    app = FastAPI(title="Pinned Mem0 benchmark server")
    configured_recall = getattr(getattr(memory, "config", None), "recall", None)
    actual_attestation = {
        "arm": arm,
        "embedder_model": str(memory.embedding_model.config.model),
        "fastapi_version": importlib.metadata.version("fastapi"),
        "llm_model": str(memory.llm.config.model),
        "llm_max_tokens": int(memory.llm.config.max_tokens),
        "llm_max_retries": int(memory.llm.client.max_retries),
        "qdrant_platform_digest": qdrant_runtime["platform_digest"],
        "recall_mode": getattr(configured_recall, "mode", "legacy"),
        "repository_revision": actual_revision,
        "runtime_packages": _installed_runtime_packages(manifest["runtimes"]["benchmark_server"]["packages"]),
        "runtime_requirements_sha256": _runtime_requirements_sha256(manifest_path),
        "store_id": config["vector_store"]["config"]["collection_name"],
        "uvicorn_version": importlib.metadata.version("uvicorn"),
    }
    if actual_attestation != expected:
        raise RuntimeError(
            f"{arm} runtime configuration does not match the manifest: "
            f"expected={json.dumps(expected, sort_keys=True)}, actual={json.dumps(actual_attestation, sort_keys=True)}"
        )

    @app.get("/benchmark-attestation")
    def benchmark_attestation() -> dict[str, Any]:
        return {
            **actual_attestation,
            "qdrant_runtime_receipt": qdrant_runtime["receipt_sha256"],
            "telemetry": telemetry.snapshot(),
        }

    @app.post("/memories")
    def add_memories(request: dict[str, Any]) -> Any:
        params = {
            key: request[key] for key in ("user_id", "agent_id", "run_id", "metadata") if request.get(key) is not None
        }
        started = time.perf_counter()
        try:
            return memory.add(request["messages"], **params)
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc
        finally:
            telemetry.add("add_requests")
            elapsed = (time.perf_counter() - started) * 1000
            telemetry.add("write_latency_ms", elapsed)
            telemetry.sample("write_latency_samples_ms", elapsed)

    @app.post("/search")
    def search_memories(request: dict[str, Any]) -> Any:
        filters, limit = _search_arguments(request)
        started = time.perf_counter()
        try:
            results = memory.search(request["query"], top_k=limit, filters=filters)
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc
        finally:
            telemetry.add("search_requests")
            elapsed = (time.perf_counter() - started) * 1000
            telemetry.add("search_latency_ms", elapsed)
            telemetry.sample("search_latency_samples_ms", elapsed)
        rows = results.get("results", results) if isinstance(results, dict) else results
        if isinstance(rows, list):
            telemetry.add("retrieved_chars", sum(len(str(item.get("memory", ""))) for item in rows))
        return results

    @app.delete("/memories")
    def delete_memories(
        user_id: str | None = Query(None),
        agent_id: str | None = Query(None),
        run_id: str | None = Query(None),
    ) -> dict[str, str]:
        params = {
            key: value for key, value in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items() if value
        }
        if not params:
            raise HTTPException(400, "one identity scope is required")
        memory.delete_all(**params)
        return {"message": "deleted"}

    @app.post("/benchmark-status")
    def benchmark_status(request: dict[str, Any]) -> dict[str, str]:
        statuses = {}
        for memory_id in request.get("ids", []):
            row = memory.vector_store.get(vector_id=str(memory_id))
            if row is not None:
                statuses[str(memory_id)] = str((row.payload or {}).get("_mem0_status", "active"))
        return statuses

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an attested Mem0 benchmark arm")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("manifest.json"))
    parser.add_argument("--work-dir", type=Path, default=Path(".evaluation-overlay"))
    parser.add_argument("--arm", required=True, choices=("baseline", "candidate", "candidate_legacy"))
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    host = manifest["arms"][args.arm]["mem0_host"]
    parsed = urllib.parse.urlparse(host)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("benchmark servers must bind to a loopback address")
    import uvicorn

    app = create_app(args.manifest.resolve(), args.work_dir.resolve(), args.arm, args.repository_root.resolve())
    uvicorn.run(app, host=parsed.hostname, port=parsed.port or 80, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
