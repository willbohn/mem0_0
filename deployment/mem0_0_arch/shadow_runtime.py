from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from deployment.mem0_0_arch.backup_restore import CommandRunner
from deployment.mem0_0_arch.source_attestation import source_attestation


class ShadowRuntimeError(RuntimeError):
    pass


SHADOW_APP_CONTAINER = "mem0_0-arch-shadow-mem0-1"


class Runner(Protocol):
    def run(self, command: Sequence[str], **kwargs: Any) -> bytes: ...


def _lower_hex(value: Any, length: int, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ShadowRuntimeError(f"{label} must be an exact lowercase digest")
    return value


def _pinned_image(value: Any, label: str) -> str:
    if not isinstance(value, str) or "@sha256:" not in value:
        raise ShadowRuntimeError(f"{label} must use an immutable sha256 registry digest")
    _lower_hex(value.rsplit("@sha256:", 1)[1], 64, label)
    return value


def start_postgres(
    *,
    compose_file: Path,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    runner = runner or CommandRunner()
    environ = os.environ if environ is None else environ
    image = _pinned_image(environ.get("MEM0_0_PGVECTOR_IMAGE"), "MEM0_0_PGVECTOR_IMAGE")
    runner.run(["podman", "compose", "-f", str(compose_file), "up", "-d", "postgres"])
    return {"schema_version": 1, "status": "started", "service": "postgres", "image": image}


def start_shadow(
    *,
    repository: Path,
    compose_file: Path,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
    attestor: Callable[[Path], dict[str, Any]] = source_attestation,
) -> dict[str, Any]:
    runner = runner or CommandRunner()
    environ = os.environ if environ is None else environ
    revision = _lower_hex(environ.get("MEM0_0_SOURCE_REVISION"), 40, "MEM0_0_SOURCE_REVISION")
    tree_digest = _lower_hex(environ.get("MEM0_0_SOURCE_TREE_SHA256"), 64, "MEM0_0_SOURCE_TREE_SHA256")
    python_image = _pinned_image(environ.get("MEM0_0_PYTHON_IMAGE"), "MEM0_0_PYTHON_IMAGE")
    _pinned_image(environ.get("MEM0_0_PGVECTOR_IMAGE"), "MEM0_0_PGVECTOR_IMAGE")
    expected = (revision, tree_digest)

    def attest() -> dict[str, Any]:
        report = attestor(repository)
        actual = (report.get("revision"), report.get("source_tree_sha256"))
        if actual != expected:
            raise ShadowRuntimeError("candidate source does not match the deployment pins")
        return report

    before = attest()
    image = f"localhost/mem0_0-arch:{revision}"
    deployment_dir = compose_file.resolve().parent
    runner.run(
        [
            "podman",
            "build",
            "--file",
            str(deployment_dir / "Containerfile"),
            "--ignorefile",
            str(deployment_dir / "containerignore"),
            "--tag",
            image,
            "--build-arg",
            f"PYTHON_IMAGE={python_image}",
            "--build-arg",
            f"SOURCE_REVISION={revision}",
            "--build-arg",
            f"SOURCE_TREE_SHA256={tree_digest}",
            str(repository.resolve()),
        ]
    )
    after = attest()
    if after != before:
        raise ShadowRuntimeError("candidate source changed while the image was being built")

    labels = (
        runner.run(
            [
                "podman",
                "image",
                "inspect",
                "--format",
                '{{ index .Labels "org.opencontainers.image.revision" }}|{{ index .Labels "io.mem0_0.source-tree-sha256" }}',
                image,
            ]
        )
        .decode()
        .strip()
    )
    if labels != f"{revision}|{tree_digest}":
        raise ShadowRuntimeError("built image labels do not match the attested candidate source")
    compose = ["podman", "compose", "-f", str(compose_file), "--profile", "application"]
    runner.run([*compose, "up", "-d", "--no-build", "--force-recreate", "mem0"])
    running_image = (
        runner.run(["podman", "inspect", "--format", "{{.ImageName}}", SHADOW_APP_CONTAINER]).decode().strip()
    )
    if running_image != image:
        raise ShadowRuntimeError("running shadow image does not match the attested candidate image")
    return {
        "schema_version": 1,
        "status": "started",
        "source_revision": revision,
        "source_tree_sha256": tree_digest,
        "image": image,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and start only an exactly attested mem0_0 shadow")
    parser.add_argument("--postgres-only", action="store_true")
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--compose-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.postgres_only:
        report = start_postgres(compose_file=args.compose_file)
    else:
        if args.repository is None:
            parser.error("--repository is required unless --postgres-only is used")
        report = start_shadow(repository=args.repository, compose_file=args.compose_file)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
