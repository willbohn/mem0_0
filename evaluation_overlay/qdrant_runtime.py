from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class QdrantRuntimeError(RuntimeError):
    pass


def _canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _single_inspection(command: list[str], label: str) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QdrantRuntimeError(f"podman returned invalid {label} inspection JSON") from exc
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise QdrantRuntimeError(f"podman returned unexpected {label} inspection data")


def verify_inspections(
    manifest: dict[str, Any],
    image: dict[str, Any],
    container: dict[str, Any],
    storage_path: Path,
) -> dict[str, Any]:
    pin = manifest["runtimes"]["qdrant"]
    expected_os, expected_architecture = pin["platform"].split("/", 1)
    if image.get("Os") != expected_os or image.get("Architecture") != expected_architecture:
        raise QdrantRuntimeError("Qdrant image platform does not match the manifest")
    repo_digests = image.get("RepoDigests") if isinstance(image.get("RepoDigests"), list) else []
    repository = pin["image"].rsplit("@", 1)[0]
    platform_reference = f"{repository}@{pin['platform_digest']}"
    if image.get("Digest") != pin["platform_digest"] and platform_reference not in repo_digests:
        raise QdrantRuntimeError("Qdrant image platform digest does not match the manifest")
    if pin["image"] not in repo_digests:
        raise QdrantRuntimeError("Qdrant image repository digest does not match the manifest")
    if container.get("Name", "").lstrip("/") != pin["container_name"]:
        raise QdrantRuntimeError("Qdrant container name does not match the manifest")
    if container.get("Image") != image.get("Id"):
        raise QdrantRuntimeError("Qdrant container image ID does not match the inspected pinned image")
    if container.get("ImageName") != pin["image"]:
        raise QdrantRuntimeError("Qdrant container image reference does not match the manifest")
    if (container.get("State") or {}).get("Status") != "running":
        raise QdrantRuntimeError("Qdrant container is not running")
    ports = ((container.get("NetworkSettings") or {}).get("Ports") or {}).get("6333/tcp") or []
    expected_port = str(pin["port"])
    if not any(
        mapping.get("HostIp") == pin["host"] and str(mapping.get("HostPort")) == expected_port
        for mapping in ports
        if isinstance(mapping, dict)
    ):
        raise QdrantRuntimeError("Qdrant container port binding does not match the manifest")
    expected_storage = str(storage_path.resolve())
    mounts = container.get("Mounts") if isinstance(container.get("Mounts"), list) else []
    if not any(
        mount.get("Destination") == "/qdrant/storage" and mount.get("Source") == expected_storage
        for mount in mounts
        if isinstance(mount, dict)
    ):
        raise QdrantRuntimeError("Qdrant container storage mount does not match the benchmark work directory")
    return {
        "container_name": pin["container_name"],
        "host": pin["host"],
        "image": pin["image"],
        "image_id": image["Id"],
        "platform": pin["platform"],
        "platform_digest": pin["platform_digest"],
        "port": pin["port"],
        "storage_path": expected_storage,
        "version": pin["version"],
    }


def inspect_runtime(manifest: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    pin = manifest["runtimes"]["qdrant"]
    image = _single_inspection(["podman", "image", "inspect", pin["image"]], "image")
    container = _single_inspection(["podman", "container", "inspect", pin["container_name"]], "container")
    return verify_inspections(manifest, image, container, work_dir / "runtime" / "qdrant" / "storage")


def _receipt_path(work_dir: Path) -> Path:
    return work_dir / "runtime" / "qdrant" / "runtime-receipt.json"


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def verify_runtime(manifest: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    path = _receipt_path(work_dir)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QdrantRuntimeError(f"Qdrant runtime has no valid receipt at {path}") from exc
    live = inspect_runtime(manifest, work_dir)
    if receipt.get("runtime") != live:
        raise QdrantRuntimeError("live Qdrant runtime does not match its pinned receipt")
    expected_fingerprint = _canonical_fingerprint(live)
    if receipt.get("fingerprint") != expected_fingerprint:
        raise QdrantRuntimeError("Qdrant runtime receipt fingerprint is invalid")
    return {**live, "receipt_sha256": _canonical_fingerprint(receipt)}


def _wait_for_qdrant(pin: dict[str, Any], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://{pin['host']}:{pin['port']}"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode())
            if str(payload.get("version")) == pin["version"]:
                return
            last_error = QdrantRuntimeError("Qdrant endpoint version does not match the manifest")
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise QdrantRuntimeError("Qdrant did not become ready with the pinned version") from last_error


def start_runtime(manifest: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    pin = manifest["runtimes"]["qdrant"]
    storage = (work_dir / "runtime" / "qdrant" / "storage").resolve()
    storage.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["podman", "pull", "--platform", pin["platform"], pin["image"]],
        check=True,
    )
    existing = subprocess.run(
        ["podman", "container", "exists", pin["container_name"]],
        check=False,
    )
    if existing.returncode != 0:
        subprocess.run(
            [
                "podman",
                "run",
                "--detach",
                "--name",
                pin["container_name"],
                "--publish",
                f"{pin['host']}:{pin['port']}:6333",
                "--volume",
                f"{storage}:/qdrant/storage:Z",
                pin["image"],
            ],
            check=True,
        )
    live = inspect_runtime(manifest, work_dir)
    _wait_for_qdrant(pin)
    receipt = {"fingerprint": _canonical_fingerprint(live), "runtime": live, "schema_version": 1}
    _write_receipt(_receipt_path(work_dir), receipt)
    return {**live, "receipt_sha256": _canonical_fingerprint(receipt)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start or verify the digest-pinned Qdrant benchmark runtime")
    parser.add_argument("command", choices=("start", "verify"))
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("manifest.json"))
    parser.add_argument("--work-dir", type=Path, default=Path(".evaluation-overlay"))
    return parser


def main(argv: list[str] | None = None) -> int:
    from evaluation_overlay.harness import load_manifest

    args = build_parser().parse_args(argv)
    manifest = load_manifest(args.manifest.resolve())
    operation = start_runtime if args.command == "start" else verify_runtime
    verified = operation(manifest, args.work_dir.resolve())
    print(json.dumps(verified, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
