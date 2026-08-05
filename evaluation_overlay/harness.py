from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import string
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
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


def _require_sha256(value: Any, path: str, *, prefix: bool = False) -> None:
    expected_length = 71 if prefix else 64
    digest = value[7:] if isinstance(value, str) and prefix and value.startswith("sha256:") else value
    if (
        not isinstance(value, str)
        or len(value) != expected_length
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in string.hexdigits for character in digest)
    ):
        suffix = "sha256:<64 hexadecimal characters>" if prefix else "64 hexadecimal characters"
        raise ManifestError(f"{path} must be {suffix}")


def _require_safe_relative_path(value: Any, path: str) -> None:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{path} is required")
    candidate = Path(value)
    if candidate.is_absolute() or value in {".", ".."} or ".." in candidate.parts:
        raise ManifestError(f"{path} must be a safe relative path")


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
        if dataset.get("source") is None:
            required_files = dataset.get("required_files")
            if not isinstance(required_files, list) or not required_files:
                raise ManifestError(f"datasets.{name}.required_files must be a non-empty array")
            if len(set(required_files)) != len(required_files):
                raise ManifestError(f"datasets.{name}.required_files must not contain duplicates")
            for index, relative_path in enumerate(required_files):
                _require_safe_relative_path(relative_path, f"datasets.{name}.required_files[{index}]")
        derivation = dataset.get("derivation")
        if derivation is not None:
            derivation = _require_mapping(derivation, f"datasets.{name}.derivation")
            if derivation.get("tool") != "duckdb" or derivation.get("binary") != "duckdb":
                raise ManifestError(f"datasets.{name}.derivation must use the pinned duckdb binary")
            _require_sha256(derivation.get("binary_sha256"), f"datasets.{name}.derivation.binary_sha256")
            _require_safe_relative_path(derivation.get("extension_path"), f"datasets.{name}.derivation.extension_path")
            _require_sha256(
                derivation.get("json_extension_sha256"),
                f"datasets.{name}.derivation.json_extension_sha256",
            )
            if not isinstance(derivation.get("version"), str) or not derivation["version"]:
                raise ManifestError(f"datasets.{name}.derivation.version is required")
            outputs = _require_mapping(derivation.get("outputs"), f"datasets.{name}.derivation.outputs")
            consumed_paths = set(outputs)
            for output, inputs in outputs.items():
                _require_safe_relative_path(output, f"datasets.{name}.derivation output")
                if not isinstance(inputs, list) or not inputs:
                    raise ManifestError(f"datasets.{name}.derivation output {output} needs input files")
                for input_path in inputs:
                    _require_safe_relative_path(input_path, f"datasets.{name}.derivation input")
                    consumed_paths.add(input_path)
            if consumed_paths != set(dataset["required_files"]):
                raise ManifestError(f"datasets.{name}.derivation inputs and outputs must equal required_files")

    models = _require_mapping(manifest.get("models"), "models")
    for name in ("llm", "embedder"):
        model = _require_mapping(models.get(name), f"models.{name}")
        if not model.get("id"):
            raise ManifestError(f"models.{name}.id is required")
        _require_revision(model.get("revision"), f"models.{name}.revision")
        artifact = _require_mapping(model.get("artifact"), f"models.{name}.artifact")
        if not isinstance(artifact.get("repository"), str) or not artifact["repository"]:
            raise ManifestError(f"models.{name}.artifact.repository is required")
        _require_revision(artifact.get("revision"), f"models.{name}.artifact.revision")
        _require_safe_relative_path(artifact.get("path"), f"models.{name}.artifact.path")
        _require_sha256(artifact.get("sha256"), f"models.{name}.artifact.sha256")
        if not isinstance(artifact.get("size"), int) or artifact["size"] <= 0:
            raise ManifestError(f"models.{name}.artifact.size must be a positive integer")
        if artifact.get("format") not in {"gguf", "safetensors"}:
            raise ManifestError(f"models.{name}.artifact.format must be gguf or safetensors")

    runtimes = _require_mapping(manifest.get("runtimes"), "runtimes")
    benchmark_server = _require_mapping(runtimes.get("benchmark_server"), "runtimes.benchmark_server")
    for package in ("fastapi", "uvicorn"):
        if not isinstance(benchmark_server.get(package), str) or not benchmark_server[package]:
            raise ManifestError(f"runtimes.benchmark_server.{package} is required")
    runtime_packages = _require_mapping(
        benchmark_server.get("packages"), "runtimes.benchmark_server.packages"
    )
    if not runtime_packages or any(
        not isinstance(package, str) or not package or not isinstance(version, str) or not version
        for package, version in runtime_packages.items()
    ):
        raise ManifestError("runtimes.benchmark_server.packages must pin every package version")
    for package in ("fastapi", "uvicorn"):
        if runtime_packages.get(package) != benchmark_server[package]:
            raise ManifestError(f"runtimes.benchmark_server.{package} must match packages")
    _require_sha256(benchmark_server.get("requirements_sha256"), "runtimes.benchmark_server.requirements_sha256")
    llama_server = _require_mapping(runtimes.get("llama_server"), "runtimes.llama_server")
    if llama_server.get("binary") != "llama-server":
        raise ManifestError("runtimes.llama_server.binary must be llama-server")
    _require_sha256(llama_server.get("sha256"), "runtimes.llama_server.sha256")
    if not isinstance(llama_server.get("version"), str) or not llama_server["version"]:
        raise ManifestError("runtimes.llama_server.version is required")
    if not isinstance(llama_server.get("device"), str) or not llama_server["device"]:
        raise ManifestError("runtimes.llama_server.device is required")
    if llama_server.get("enable_thinking") is not False:
        raise ManifestError("runtimes.llama_server.enable_thinking must be false")
    if llama_server.get("gpu_layers") != "all":
        raise ManifestError("runtimes.llama_server.gpu_layers must be all")
    qdrant = _require_mapping(runtimes.get("qdrant"), "runtimes.qdrant")
    image = qdrant.get("image")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise ManifestError("runtimes.qdrant.image must be digest-pinned")
    _require_sha256(image.rsplit("@", 1)[-1], "runtimes.qdrant.image digest", prefix=True)
    _require_sha256(qdrant.get("platform_digest"), "runtimes.qdrant.platform_digest", prefix=True)
    if qdrant.get("platform") not in {"linux/amd64", "linux/arm64"}:
        raise ManifestError("runtimes.qdrant.platform must be linux/amd64 or linux/arm64")
    if not isinstance(qdrant.get("version"), str) or not qdrant["version"]:
        raise ManifestError("runtimes.qdrant.version is required")
    if qdrant.get("container_name") != "mem0_0-benchmark-qdrant":
        raise ManifestError("runtimes.qdrant.container_name must be mem0_0-benchmark-qdrant")
    if qdrant.get("host") not in {"127.0.0.1", "::1"}:
        raise ManifestError("runtimes.qdrant.host must be a loopback address")
    if not isinstance(qdrant.get("port"), int) or not 1 <= qdrant["port"] <= 65535:
        raise ManifestError("runtimes.qdrant.port must be between 1 and 65535")

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


_DATASET_RECEIPT_KEYS = (
    "kind",
    "repository",
    "revision",
    "required_files",
    "additional_repository",
    "additional_revision",
    "derivation",
)


def _expected_derivation_proof(derivation: dict[str, Any]) -> dict[str, Any]:
    return {
        "binary_sha256": derivation["binary_sha256"],
        "extension_sha256": derivation["json_extension_sha256"],
        "kind": "mem0_0_executed_derivation_v1",
        "outputs": derivation["outputs"],
        "version": derivation["version"],
    }


def _dataset_receipt(dataset: dict[str, Any], files: dict[str, str], **extra: Any) -> dict[str, Any]:
    receipt = {key: dataset[key] for key in _DATASET_RECEIPT_KEYS if key in dataset}
    receipt["files"] = files
    receipt.update(extra)
    return receipt


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
        expected = {key: dataset[key] for key in _DATASET_RECEIPT_KEYS if key in dataset}
        actual = {key: receipt.get(key) for key in expected}
        if actual != expected:
            raise ManifestError(f"dataset {name} receipt does not match the pinned manifest")
        if set(receipt.get("files") or {}) != set(dataset["required_files"]):
            raise ManifestError(f"dataset {name} receipt files must exactly match required_files")
        if dataset.get("derivation"):
            if receipt.get("derivation_proof") != _expected_derivation_proof(dataset["derivation"]):
                raise ManifestError(f"dataset {name} has no valid executed derivation proof")
        elif receipt.get("derivation_proof") is not None:
            raise ManifestError(f"dataset {name} has an unexpected derivation proof")
        files = _verify_file_hashes(receipt_path.parent, receipt.get("files"), f"dataset {name}")
        verified[name] = {
            "files": files,
            "receipt_sha256": _fingerprint(receipt),
            "revision": dataset["revision"],
        }
    return verified


def prepare_dataset_receipt(
    manifest: dict[str, Any], work_dir: Path, name: str, relative_paths: list[str]
) -> dict[str, Any]:
    validate_manifest(manifest)
    if name not in manifest["datasets"]:
        raise ManifestError(f"unknown dataset {name!r}")
    dataset = manifest["datasets"][name]
    if dataset.get("source"):
        raise ManifestError(f"dataset {name} is verified from its pinned Git source and needs no receipt")
    if dataset.get("derivation"):
        raise ManifestError(f"dataset {name} must be prepared with its pinned derivation command")
    required_files = set(dataset["required_files"])
    if set(relative_paths) != required_files or len(relative_paths) != len(required_files):
        raise ManifestError(f"dataset {name} files must exactly match required_files")
    dataset_dir = (work_dir / "datasets" / name).resolve()
    files: dict[str, str] = {}
    for relative_path in sorted(set(relative_paths)):
        _require_safe_relative_path(relative_path, f"dataset {name} file")
        candidate = (dataset_dir / relative_path).resolve()
        if candidate == dataset_dir or dataset_dir not in candidate.parents:
            raise ManifestError(f"dataset {name} contains an unsafe file path")
        try:
            files[relative_path] = _sha256_file(candidate)
        except OSError as exc:
            raise ManifestError(f"dataset {name} is missing consumed file {relative_path}") from exc
    receipt = _dataset_receipt(dataset, files)
    _atomic_write_json(dataset_dir / ".source-receipt.json", receipt)
    return receipt


def derive_dataset_receipt(manifest: dict[str, Any], work_dir: Path, name: str) -> dict[str, Any]:
    """Execute a manifest-pinned DuckDB Parquet-to-JSON derivation and receipt every consumed file."""
    validate_manifest(manifest)
    if name not in manifest["datasets"]:
        raise ManifestError(f"unknown dataset {name!r}")
    dataset = manifest["datasets"][name]
    derivation = dataset.get("derivation")
    if not derivation:
        raise ManifestError(f"dataset {name} has no declared derivation")

    binary_name = derivation["binary"]
    resolved_binary = shutil.which(binary_name)
    if not resolved_binary:
        raise ManifestError(f"pinned derivation binary {binary_name} is not installed")
    binary = Path(resolved_binary).resolve()
    try:
        binary_sha256 = _sha256_file(binary)
    except OSError as exc:
        raise ManifestError("cannot read the pinned derivation binary") from exc
    if binary_sha256 != derivation["binary_sha256"]:
        raise ManifestError("installed derivation binary does not match the pinned SHA-256")
    version = subprocess.run([str(binary), "-version"], check=True, capture_output=True, text=True).stdout.strip()
    if version != derivation["version"]:
        raise ManifestError("installed derivation binary version does not match the manifest")

    extension = (work_dir / derivation["extension_path"]).resolve()
    try:
        extension_sha256 = _sha256_file(extension)
    except OSError as exc:
        raise ManifestError("cannot read the pinned DuckDB JSON extension") from exc
    if work_dir.resolve() not in extension.parents or extension_sha256 != derivation["json_extension_sha256"]:
        raise ManifestError("DuckDB JSON extension does not match the pinned SHA-256")

    dataset_dir = (work_dir / "datasets" / name).resolve()
    dataset_dir.mkdir(parents=True, exist_ok=True)

    def sql_path(path: Path) -> str:
        return str(path).replace("'", "''")

    for output_path, input_paths in sorted(derivation["outputs"].items()):
        output = (dataset_dir / output_path).resolve()
        inputs = [(dataset_dir / input_path).resolve() for input_path in input_paths]
        for input_path in inputs:
            if dataset_dir not in input_path.parents or not input_path.is_file():
                raise ManifestError(f"dataset {name} is missing derivation input {input_path.name}")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        parquet_argument = (
            f"'{sql_path(inputs[0])}'"
            if len(inputs) == 1
            else "[" + ",".join(f"'{sql_path(input_path)}'" for input_path in inputs) + "]"
        )
        statement = (
            f"LOAD '{sql_path(extension)}'; "
            f"COPY (SELECT * FROM read_parquet({parquet_argument})) "
            f"TO '{sql_path(temporary)}' (FORMAT JSON, ARRAY true);"
        )
        try:
            subprocess.run([str(binary), "-unsigned", "-c", statement], check=True, capture_output=True, text=True)
            os.replace(temporary, output)
        except (OSError, subprocess.CalledProcessError) as exc:
            temporary.unlink(missing_ok=True)
            raise ManifestError(f"dataset {name} derivation failed for {output_path}") from exc

    files = {relative_path: _sha256_file(dataset_dir / relative_path) for relative_path in dataset["required_files"]}
    receipt = _dataset_receipt(dataset, files, derivation_proof=_expected_derivation_proof(derivation))
    _atomic_write_json(dataset_dir / ".source-receipt.json", receipt)
    return receipt


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
        if receipt.get("artifact") != model["artifact"]:
            raise ManifestError(f"model {name} receipt does not match the artifact pin")
        expected_files = {model["artifact"]["path"]: model["artifact"]["sha256"]}
        if receipt.get("files") != expected_files:
            raise ManifestError(f"model {name} receipt files do not match the artifact pin")
        files = _verify_file_hashes(receipt_path.parent, receipt.get("files"), f"model {name}")
        artifact_path = receipt_path.parent / model["artifact"]["path"]
        if artifact_path.stat().st_size != model["artifact"]["size"]:
            raise ManifestError(f"model {name} artifact size does not match the manifest")
        verified[name] = {
            "artifact": model["artifact"],
            "files": files,
            "receipt_sha256": _fingerprint(receipt),
            "revision": model["revision"],
        }
    return verified


def materialize_model_artifact(
    manifest: dict[str, Any],
    work_dir: Path,
    name: str,
    source_path: Path,
    *,
    allow_copy: bool = False,
) -> dict[str, Any]:
    """Hard-link one already downloaded, exactly pinned model artifact into the private benchmark work tree."""
    validate_manifest(manifest)
    if name not in {"llm", "embedder"}:
        raise ManifestError(f"unknown model {name!r}")
    model = manifest["models"][name]
    artifact = model["artifact"]
    source_path = source_path.resolve()
    try:
        source_size = source_path.stat().st_size
        source_sha256 = _sha256_file(source_path)
    except OSError as exc:
        raise ManifestError(f"model {name} source artifact is unavailable at {source_path}") from exc
    if source_size != artifact["size"] or source_sha256 != artifact["sha256"]:
        raise ManifestError(f"model {name} source does not match the pinned artifact")

    model_dir = (work_dir / "models" / name).resolve()
    target_path = model_dir / artifact["path"]
    model_dir.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        if target_path.stat().st_size != source_size or _sha256_file(target_path) != source_sha256:
            raise ManifestError(f"model {name} target exists with different content")
    else:
        try:
            os.link(source_path, target_path)
        except OSError as exc:
            if not allow_copy:
                raise ManifestError(
                    f"cannot hard-link model {name} into {model_dir}; rerun with explicit copy approval"
                ) from exc
            temporary_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
            try:
                with source_path.open("rb") as source, temporary_path.open("xb") as target:
                    shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                if temporary_path.stat().st_size != source_size or _sha256_file(temporary_path) != source_sha256:
                    raise ManifestError(f"copied model {name} failed post-copy verification")
                os.replace(temporary_path, target_path)
            finally:
                temporary_path.unlink(missing_ok=True)

    receipt = {
        "artifact": artifact,
        "files": {artifact["path"]: artifact["sha256"]},
        "id": model["id"],
        "revision": model["revision"],
    }
    _atomic_write_json(model_dir / ".source-receipt.json", receipt)
    return receipt


def expected_runtime_attestation(manifest: dict[str, Any], arm_name: str) -> dict[str, Any]:
    arm = manifest["arms"][arm_name]
    revision_key = "baseline_revision" if arm_name == "baseline" else "candidate_revision"
    return {
        "arm": arm_name,
        "fastapi_version": manifest["runtimes"]["benchmark_server"]["fastapi"],
        "embedder_model": manifest["models"]["embedder"]["id"],
        "llm_model": manifest["models"]["llm"]["id"],
        "qdrant_platform_digest": manifest["runtimes"]["qdrant"]["platform_digest"],
        "recall_mode": arm["recall_mode"],
        "repository_revision": manifest["repository"][revision_key],
        "runtime_packages": manifest["runtimes"]["benchmark_server"]["packages"],
        "runtime_requirements_sha256": manifest["runtimes"]["benchmark_server"]["requirements_sha256"],
        "store_id": arm["store_id"],
        "uvicorn_version": manifest["runtimes"]["benchmark_server"]["uvicorn"],
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
        parsed = urllib.parse.urlsplit(base_url)
        try:
            is_loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            is_loopback = False
        if parsed.scheme != "http" or not is_loopback:
            raise ManifestError(f"{variable_name} must be an HTTP loopback endpoint")
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
        attestation_request = urllib.request.Request(
            f"{base_url.rstrip('/')}/mem0_0/attestation",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(attestation_request, timeout=10) as response:
                attestation = json.loads(response.read().decode())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot verify the mem0_0 proxy attestation for {name}") from exc
        expected_attestation = {
            "artifact_sha256": manifest["models"][name]["artifact"]["sha256"],
            "backend_verified": True,
            "kind": "mem0_0_model_proxy_v1",
            "device": manifest["runtimes"]["llama_server"]["device"],
            "enable_thinking": manifest["runtimes"]["llama_server"]["enable_thinking"],
            "gpu_layers": manifest["runtimes"]["llama_server"]["gpu_layers"],
            "launcher_sha256": manifest["runtimes"]["llama_server"]["sha256"],
            "model_id": model_id,
            "offloaded_layers": attestation.get("offloaded_layers"),
            "receipt_sha256": expected_fingerprint,
            "resolved_device": manifest["runtimes"]["llama_server"]["device"],
            "total_layers": attestation.get("total_layers"),
        }
        if (
            not isinstance(attestation.get("offloaded_layers"), int)
            or attestation["offloaded_layers"] <= 0
            or attestation.get("total_layers") != attestation["offloaded_layers"]
        ):
            raise ManifestError(f"{name} endpoint did not verify complete model-layer offload")
        if attestation != expected_attestation:
            raise ManifestError(f"{name} endpoint is not the receipt-bound mem0_0 model proxy")
        verified[name] = {
            "base_url": base_url.rstrip("/"),
            "model_id": model_id,
            "proxy_attestation": "mem0_0_model_proxy_v1",
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
