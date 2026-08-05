from __future__ import annotations

import hashlib
import json
import os
import string
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REQUIRED_ARMS = {"baseline", "candidate", "candidate_legacy"}
REQUIRED_BENCHMARKS = {"locomo", "longmemeval", "beam", "stale", "memora"}
ALLOWED_PLACEHOLDERS = {
    "benchmark",
    "dataset_dir",
    "max_workers",
    "mem0_host",
    "output_dir",
    "project_name",
    "python",
    "resume_flag",
    "run_id",
    "seed",
    "side",
    "source_dir",
    "top_k",
    "top_k_cutoffs",
}


class ManifestError(ValueError):
    pass


class CheckpointMismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunSpec:
    side: str
    benchmark: str
    fingerprint: str
    run_id: str
    command: tuple[str, ...]
    environment: dict[str, str]
    source_dir: Path
    dataset_dir: Path
    output_dir: Path
    state_path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class RunResult:
    status: str
    returncode: int
    state_path: Path


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_revision(value: Any, path: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(character not in string.hexdigits for character in value):
        raise ManifestError(f"{path} must be a full 40-character hexadecimal revision")


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{path} must be an object")
    return value


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")

    repository = _require_mapping(manifest.get("repository"), "repository")
    _require_revision(repository.get("baseline_revision"), "repository.baseline_revision")
    _require_revision(repository.get("candidate_revision"), "repository.candidate_revision")

    sources = _require_mapping(manifest.get("sources"), "sources")
    for name, source in sources.items():
        source = _require_mapping(source, f"sources.{name}")
        if source.get("kind") != "git":
            raise ManifestError(f"sources.{name}.kind must be git")
        _require_revision(source.get("revision"), f"sources.{name}.revision")
        if not source.get("location") or not source.get("repository"):
            raise ManifestError(f"sources.{name} requires location and repository")
        if source.get("location_root", "repository") not in {"repository", "work"}:
            raise ManifestError(f"sources.{name}.location_root must be repository or work")

    datasets = _require_mapping(manifest.get("datasets"), "datasets")
    missing_datasets = REQUIRED_BENCHMARKS - set(datasets)
    if missing_datasets:
        raise ManifestError(f"datasets missing required entries: {', '.join(sorted(missing_datasets))}")
    for name, dataset in datasets.items():
        dataset = _require_mapping(dataset, f"datasets.{name}")
        _require_revision(dataset.get("revision"), f"datasets.{name}.revision")
        if dataset.get("additional_revision") is not None:
            _require_revision(dataset.get("additional_revision"), f"datasets.{name}.additional_revision")
        if not dataset.get("repository") or dataset.get("kind") not in {"git_file", "huggingface", "git"}:
            raise ManifestError(f"datasets.{name} requires a supported kind and repository")
        if dataset.get("source") is not None and dataset["source"] not in sources:
            raise ManifestError(f"datasets.{name}.source is not declared")

    models = _require_mapping(manifest.get("models"), "models")
    for name in ("llm", "embedder"):
        model = _require_mapping(models.get(name), f"models.{name}")
        if not model.get("id"):
            raise ManifestError(f"models.{name}.id is required")
        _require_revision(model.get("revision"), f"models.{name}.revision")

    arms = _require_mapping(manifest.get("arms"), "arms")
    if set(arms) != REQUIRED_ARMS:
        missing = REQUIRED_ARMS - set(arms)
        extra = set(arms) - REQUIRED_ARMS
        raise ManifestError(
            f"arms must be exactly {sorted(REQUIRED_ARMS)}; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    hosts: set[str] = set()
    stores: set[str] = set()
    for name, arm in arms.items():
        arm = _require_mapping(arm, f"arms.{name}")
        host = arm.get("mem0_host")
        store_id = arm.get("store_id")
        if not isinstance(host, str) or not host.startswith(("http://", "https://")):
            raise ManifestError(f"arms.{name}.mem0_host must be an HTTP(S) URL")
        if not isinstance(store_id, str) or not store_id:
            raise ManifestError(f"arms.{name}.store_id is required")
        expected_mode = "state_aware" if name == "candidate" else "legacy"
        if arm.get("recall_mode") != expected_mode:
            raise ManifestError(f"arms.{name}.recall_mode must be {expected_mode}")
        hosts.add(host)
        stores.add(store_id)
    if len(hosts) != len(arms) or len(stores) != len(arms):
        raise ManifestError("each arm must use a distinct mem0_host and store_id")

    benchmarks = _require_mapping(manifest.get("benchmarks"), "benchmarks")
    missing_benchmarks = REQUIRED_BENCHMARKS - set(benchmarks)
    if missing_benchmarks:
        raise ManifestError(f"benchmarks missing required entries: {', '.join(sorted(missing_benchmarks))}")
    for name, benchmark in benchmarks.items():
        benchmark = _require_mapping(benchmark, f"benchmarks.{name}")
        if benchmark.get("source") not in sources:
            raise ManifestError(f"benchmarks.{name}.source is not declared")
        if benchmark.get("dataset") not in datasets:
            raise ManifestError(f"benchmarks.{name}.dataset is not declared")
        command = benchmark.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ManifestError(f"benchmarks.{name}.command must be a non-empty string array")
        for part in command:
            for _, field_name, _, _ in string.Formatter().parse(part):
                if field_name and field_name not in ALLOWED_PLACEHOLDERS:
                    raise ManifestError(f"benchmarks.{name}.command uses unsupported placeholder {field_name!r}")

    if not isinstance(manifest.get("budgets"), dict) or not manifest["budgets"]:
        raise ManifestError("budgets must be a non-empty object")
    budgets = manifest["budgets"]
    for key in ("seed", "top_k", "max_workers"):
        if not isinstance(budgets.get(key), int) or budgets[key] < (0 if key == "seed" else 1):
            raise ManifestError(f"budgets.{key} must be a valid integer")
    cutoffs = budgets.get("top_k_cutoffs")
    if (
        not isinstance(cutoffs, list)
        or not cutoffs
        or not all(isinstance(value, int) and value > 0 for value in cutoffs)
    ):
        raise ManifestError("budgets.top_k_cutoffs must be a non-empty positive integer array")
    if not isinstance(manifest.get("gates"), dict) or not manifest["gates"]:
        raise ManifestError("gates must be a non-empty object")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot load manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    validate_manifest(manifest)
    return manifest


def _source_location(source: dict[str, Any], repository_root: Path, work_dir: Path | None = None) -> Path:
    location_root = source.get("location_root", "repository")
    if location_root == "repository":
        base = repository_root
    elif location_root == "work" and work_dir is not None:
        base = work_dir
    else:
        raise ManifestError(f"source location_root {location_root!r} requires a work directory")
    return (base / source["location"]).resolve()


def verify_sources(manifest: dict[str, Any], repository_root: Path, *, work_dir: Path | None = None) -> dict[str, str]:
    verified: dict[str, str] = {}
    for name, source in manifest["sources"].items():
        location = _source_location(source, repository_root.resolve(), work_dir.resolve() if work_dir else None)
        if not location.is_dir():
            raise ManifestError(f"source {name} is missing at {location}")
        revision = subprocess.run(
            ["git", "-C", str(location), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if revision != source["revision"]:
            raise ManifestError(f"source {name} expected {source['revision']} but found {revision}")
        dirty = subprocess.run(
            ["git", "-C", str(location), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            raise ManifestError(f"source {name} has tracked local changes")
        verified[name] = revision
    return verified


def _verify_file_hashes(root: Path, files: Any, artifact: str) -> dict[str, str]:
    if not isinstance(files, dict) or not files:
        raise ManifestError(f"{artifact} receipt must contain at least one SHA-256 file entry")
    verified: dict[str, str] = {}
    root = root.resolve()
    for relative_path, expected_digest in sorted(files.items()):
        if not isinstance(relative_path, str) or not isinstance(expected_digest, str):
            raise ManifestError(f"{artifact} receipt file entries must be strings")
        candidate = (root / relative_path).resolve()
        if candidate == root or root not in candidate.parents:
            raise ManifestError(f"{artifact} receipt contains an unsafe path")
        if len(expected_digest) != 64 or any(character not in string.hexdigits for character in expected_digest):
            raise ManifestError(f"{artifact} receipt has an invalid SHA-256 for {relative_path}")
        try:
            digest = _sha256_file(candidate)
        except OSError as exc:
            raise ManifestError(f"{artifact} is missing pinned file {relative_path}") from exc
        if digest != expected_digest.casefold():
            raise ManifestError(f"{artifact} file {relative_path} failed SHA-256 verification")
        verified[relative_path] = digest
    return verified


def verify_dataset_receipts(manifest: dict[str, Any], work_dir: Path) -> dict[str, dict[str, Any]]:
    verified: dict[str, dict[str, Any]] = {}
    for name, dataset in manifest["datasets"].items():
        source_name = dataset.get("source")
        if source_name:
            source = manifest["sources"][source_name]
            if source["revision"] != dataset["revision"] or source["repository"] != dataset["repository"]:
                raise ManifestError(f"dataset {name} does not match its pinned Git source")
            verified[name] = {"revision": dataset["revision"], "source": source_name}
            continue
        receipt_path = work_dir / "datasets" / name / ".source-receipt.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"dataset {name} has no valid source receipt at {receipt_path}") from exc
        expected = {key: dataset[key] for key in ("kind", "repository", "revision")}
        actual = {key: receipt.get(key) for key in expected}
        if actual != expected:
            raise ManifestError(f"dataset {name} receipt does not match the pinned manifest")
        files = _verify_file_hashes(receipt_path.parent, receipt.get("files"), f"dataset {name}")
        verified[name] = {
            "files": files,
            "receipt_sha256": _fingerprint(receipt),
            "revision": dataset["revision"],
        }
    return verified


def verify_model_receipts(manifest: dict[str, Any], work_dir: Path) -> dict[str, dict[str, Any]]:
    verified: dict[str, dict[str, Any]] = {}
    for name, model in manifest["models"].items():
        receipt_path = work_dir / "models" / name / ".source-receipt.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"model {name} has no valid source receipt at {receipt_path}") from exc
        expected = {key: model[key] for key in ("id", "revision")}
        actual = {key: receipt.get(key) for key in expected}
        if actual != expected:
            raise ManifestError(f"model {name} receipt does not match the pinned manifest")
        files = _verify_file_hashes(receipt_path.parent, receipt.get("files"), f"model {name}")
        verified[name] = {
            "files": files,
            "receipt_sha256": _fingerprint(receipt),
            "revision": model["revision"],
        }
    return verified


def expected_runtime_attestation(manifest: dict[str, Any], arm_name: str) -> dict[str, Any]:
    arm = manifest["arms"][arm_name]
    revision_key = "baseline_revision" if arm_name == "baseline" else "candidate_revision"
    return {
        "arm": arm_name,
        "embedder_model": manifest["models"]["embedder"]["id"],
        "llm_model": manifest["models"]["llm"]["id"],
        "recall_mode": arm["recall_mode"],
        "repository_revision": manifest["repository"][revision_key],
        "store_id": arm["store_id"],
    }


def get_runtime_attestation(manifest: dict[str, Any], arm_name: str) -> dict[str, Any]:
    host = manifest["arms"][arm_name]["mem0_host"].rstrip("/")
    try:
        with urllib.request.urlopen(f"{host}/benchmark-attestation", timeout=10) as response:
            value = json.loads(response.read().decode())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot attest runtime {arm_name} at {host}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"runtime {arm_name} returned an invalid attestation")
    return value


def verify_runtime_attestation(manifest: dict[str, Any], arm_name: str) -> dict[str, Any]:
    actual = get_runtime_attestation(manifest, arm_name)
    expected = expected_runtime_attestation(manifest, arm_name)
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise ManifestError(f"runtime {arm_name} attestation mismatch: {json.dumps(mismatches, sort_keys=True)}")
    if not isinstance(actual.get("telemetry"), dict):
        raise ManifestError(f"runtime {arm_name} attestation has no telemetry")
    return actual


def verify_model_endpoints(
    manifest: dict[str, Any], verified_models: dict[str, dict[str, Any]]
) -> dict[str, dict[str, str]]:
    api_key = os.environ.get("MEM0_BENCH_LOCAL_API_KEY")
    endpoint_specs = {
        "llm": (
            "MEM0_BENCH_LLM_BASE_URL",
            os.environ.get("MEM0_BENCH_LLM_BASE_URL"),
            manifest["models"]["llm"]["id"],
        ),
        "embedder": (
            "MEM0_BENCH_EMBED_BASE_URL",
            os.environ.get("MEM0_BENCH_EMBED_BASE_URL"),
            manifest["models"]["embedder"]["id"],
        ),
    }
    if not api_key:
        raise ManifestError("MEM0_BENCH_LOCAL_API_KEY is required for local model endpoint verification")
    verified: dict[str, dict[str, str]] = {}
    for name, (variable_name, base_url, model_id) in endpoint_specs.items():
        if not base_url:
            raise ManifestError(f"{variable_name} is required")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot verify {name} model endpoint at {base_url}") from exc
        expected_fingerprint = str((verified_models.get(name) or {}).get("receipt_sha256", ""))
        if len(expected_fingerprint) != 64:
            raise ManifestError(f"verified model receipt fingerprint is missing for {name}")
        model_entry = next(
            (
                item
                for item in (payload.get("data") or [])
                if isinstance(item, dict) and str(item.get("id")) == model_id
            ),
            None,
        )
        if model_entry is None:
            raise ManifestError(f"{name} endpoint does not advertise pinned model {model_id}")
        metadata = model_entry.get("metadata") if isinstance(model_entry.get("metadata"), dict) else {}
        actual_fingerprint = metadata.get("mem0_0_artifact_fingerprint")
        if actual_fingerprint != expected_fingerprint:
            raise ManifestError(
                f"{name} endpoint artifact fingerprint does not match the locally verified model receipt"
            )
        verified[name] = {
            "base_url": base_url.rstrip("/"),
            "model_id": model_id,
            "receipt_sha256": expected_fingerprint,
        }
    return verified


def _expand_command(command: list[str], values: dict[str, str]) -> tuple[str, ...]:
    expanded = tuple(part.format_map(values) for part in command)
    return tuple(part for part in expanded if part)


def build_run_spec(
    manifest: dict[str, Any],
    work_dir: Path,
    side: str,
    benchmark: str,
    *,
    repository_root: Path | None = None,
    resume: bool = False,
    require_provider_environment: bool = False,
) -> RunSpec:
    validate_manifest(manifest)
    if side not in manifest["arms"]:
        raise ManifestError(f"unknown arm {side!r}")
    if benchmark not in manifest["benchmarks"]:
        raise ManifestError(f"unknown benchmark {benchmark!r}")

    repository_root = (repository_root or Path.cwd()).resolve()
    work_dir = work_dir.resolve()
    arm = manifest["arms"][side]
    benchmark_config = manifest["benchmarks"][benchmark]
    source = manifest["sources"][benchmark_config["source"]]
    dataset_name = benchmark_config["dataset"]
    source_dir = _source_location(source, repository_root, work_dir)
    dataset_dir = work_dir / "datasets" / dataset_name
    output_dir = work_dir / "runs" / side / benchmark
    fingerprint = _fingerprint({"manifest": manifest, "side": side, "benchmark": benchmark})
    run_id = fingerprint[:16]
    values = {
        "benchmark": benchmark,
        "dataset_dir": str(dataset_dir),
        "mem0_host": arm["mem0_host"],
        "output_dir": str(output_dir / "output"),
        "project_name": f"mem0_0-{side}-{benchmark}-{run_id[:8]}",
        "python": sys.executable,
        "resume_flag": "--resume" if resume else "",
        "run_id": run_id,
        "seed": str(manifest["budgets"].get("seed", 0)),
        "side": side,
        "source_dir": str(source_dir),
        "top_k": str(manifest["budgets"].get("top_k", 200)),
        "top_k_cutoffs": ",".join(str(value) for value in manifest["budgets"].get("top_k_cutoffs", [])),
        "max_workers": str(manifest["budgets"].get("max_workers", 1)),
    }
    command = _expand_command(benchmark_config["command"], values)
    environment = {
        "MEM0_BENCH_ARM": side,
        "MEM0_BENCH_BENCHMARK": benchmark,
        "MEM0_BENCH_STORE_ID": arm["store_id"],
        "MEM0_DIR": str(work_dir / "runtime" / side / ".mem0_0"),
        "MEM0_HOST": arm["mem0_host"],
        "PYTHONPATH": str(repository_root),
        "PYTHONHASHSEED": str(manifest["budgets"].get("seed", 0)),
    }
    if require_provider_environment:
        for source_name, destination_name in (
            ("MEM0_BENCH_LLM_BASE_URL", "OPENAI_BASE_URL"),
            ("MEM0_BENCH_LOCAL_API_KEY", "OPENAI_API_KEY"),
        ):
            value = os.environ.get(source_name)
            if not value:
                raise ManifestError(f"{source_name} is required for local answerer and judge execution")
            environment[destination_name] = value
    return RunSpec(
        side=side,
        benchmark=benchmark,
        fingerprint=fingerprint,
        run_id=run_id,
        command=command,
        environment=environment,
        source_dir=source_dir,
        dataset_dir=dataset_dir,
        output_dir=output_dir,
        state_path=output_dir / "run-state.json",
        manifest=manifest,
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointMismatchError(f"invalid checkpoint at {path}") from exc
    if not isinstance(value, dict):
        raise CheckpointMismatchError(f"invalid checkpoint at {path}")
    return value


def _state_payload(
    spec: RunSpec,
    status: str,
    returncode: int | None = None,
    *,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "fingerprint": spec.fingerprint,
        "run_id": spec.run_id,
        "arm": spec.side,
        "benchmark": spec.benchmark,
        "status": status,
        "returncode": returncode,
        "command": list(spec.command),
        "environment_keys": sorted(spec.environment),
    }
    if telemetry is not None:
        payload["telemetry"] = telemetry
    return payload


def _telemetry_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keys = set(before) | set(after)
    delta: dict[str, Any] = {}
    for key in sorted(keys):
        before_value = before.get(key, 0)
        after_value = after.get(key, 0)
        if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
            delta[key] = float(after_value) - float(before_value)
        elif isinstance(before_value, list) and isinstance(after_value, list):
            if after_value[: len(before_value)] != before_value:
                raise ManifestError(f"runtime telemetry {key} was reset or changed during a run")
            delta[key] = after_value[len(before_value) :]
    return delta


def run_benchmark(spec: RunSpec, *, resume: bool = False, dry_run: bool = False) -> RunResult:
    existing = _read_state(spec.state_path)
    if existing and existing.get("fingerprint") != spec.fingerprint:
        raise CheckpointMismatchError(
            f"checkpoint {spec.state_path} belongs to a different manifest; use a new work directory"
        )
    if existing and existing.get("status") == "completed":
        if resume:
            return RunResult("skipped", int(existing.get("returncode") or 0), spec.state_path)
        raise FileExistsError(f"completed run already exists at {spec.state_path}; pass resume=True to skip it")
    if existing and not resume:
        raise FileExistsError(f"incomplete run exists at {spec.state_path}; pass resume=True to continue it")
    if dry_run:
        return RunResult("planned", 0, spec.state_path)

    before = verify_runtime_attestation(spec.manifest, spec.side)
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    Path(spec.environment["MEM0_DIR"]).mkdir(parents=True, exist_ok=True)
    _atomic_write_json(spec.state_path, _state_payload(spec, "running"))
    environment = os.environ.copy()
    environment.update(spec.environment)
    stdout_path = spec.output_dir / "stdout.log"
    stderr_path = spec.output_dir / "stderr.log"
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        completed = subprocess.run(
            list(spec.command),
            cwd=spec.source_dir,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    after = verify_runtime_attestation(spec.manifest, spec.side)
    telemetry = _telemetry_delta(before["telemetry"], after["telemetry"])
    status = "completed" if completed.returncode == 0 else "failed"
    _atomic_write_json(spec.state_path, _state_payload(spec, status, completed.returncode, telemetry=telemetry))
    if completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, spec.command)
    return RunResult(status, completed.returncode, spec.state_path)
