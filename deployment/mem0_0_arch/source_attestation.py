from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterable, NamedTuple


class SourceAttestationError(RuntimeError):
    pass


class TrackedEntry(NamedTuple):
    mode: str
    object_id: str
    path: str


def tracked_tree_fingerprint(root: Path, entries: Iterable[TrackedEntry]) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    for entry in sorted(set(entries), key=lambda value: value.path):
        relative_path = entry.path
        path_part = Path(relative_path)
        if path_part.is_absolute() or ".." in path_part.parts or path_part == Path("."):
            raise SourceAttestationError("tracked source path escapes the repository")
        lexical_candidate = root / path_part
        candidate = lexical_candidate.resolve()
        if entry.mode != "120000" and (candidate == root or root not in candidate.parents):
            raise SourceAttestationError("tracked source path escapes the repository")
        if entry.mode == "160000":
            try:
                content_digest = bytes.fromhex(entry.object_id)
            except ValueError as exc:
                raise SourceAttestationError("Gitlink has an invalid object ID") from exc
        else:
            try:
                content = os.readlink(lexical_candidate).encode() if entry.mode == "120000" else candidate.read_bytes()
            except OSError as exc:
                raise SourceAttestationError(f"tracked source file is unavailable: {relative_path}") from exc
            content_digest = hashlib.sha256(content).digest()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(entry.mode.encode())
        digest.update(b"\0")
        digest.update(content_digest)
    return digest.hexdigest()


def _git(repository: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceAttestationError(f"cannot attest Git source with command {args[0]}") from exc


def source_attestation(repository: Path) -> dict[str, Any]:
    revision = _git(repository, "rev-parse", "HEAD").decode().strip()
    if len(revision) != 40:
        raise SourceAttestationError("Git source has no full revision")
    entries = []
    for value in _git(repository, "ls-files", "--stage", "-z").split(b"\0"):
        if not value:
            continue
        try:
            metadata, raw_path = value.split(b"\t", 1)
            mode, object_id, stage = metadata.decode().split()
            path = raw_path.decode()
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceAttestationError("Git returned an invalid tracked-file entry") from exc
        if stage != "0":
            raise SourceAttestationError("cannot attest a source tree with unresolved index stages")
        entries.append(TrackedEntry(mode, object_id, path))
    changed_tracked = [
        line for line in _git(repository, "status", "--porcelain", "--untracked-files=no").splitlines() if line
    ]
    untracked_paths = [
        value.decode()
        for value in _git(repository, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if value
    ]
    for path in untracked_paths:
        try:
            file_mode = (repository / path).lstat().st_mode
        except OSError as exc:
            raise SourceAttestationError(f"untracked source file is unavailable: {path}") from exc
        if stat.S_ISLNK(file_mode):
            mode = "120000"
        elif stat.S_ISREG(file_mode):
            mode = "100755" if file_mode & stat.S_IXUSR else "100644"
        else:
            raise SourceAttestationError(f"untracked source path is not a regular file: {path}")
        entries.append(TrackedEntry(mode, "0" * 40, path))
    return {
        "schema_version": 1,
        "revision": revision,
        "tracked_files": len(entries) - len(untracked_paths),
        "changed_tracked_files": len(changed_tracked),
        "untracked_files": len(untracked_paths),
        "clean": not changed_tracked and not untracked_paths,
        "source_tree_sha256": tracked_tree_fingerprint(repository, entries),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Attest a deployed Mem0 source tree without exposing source content")
    parser.add_argument("repository", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(source_attestation(args.repository), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
