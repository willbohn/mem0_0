from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MigrationConflictError(RuntimeError):
    """Raised before a migration step would overwrite or mix state."""


@dataclass(frozen=True)
class RuntimePaths:
    legacy_state: Path
    legacy_config: Path
    target_state: Path
    target_config: Path

    @classmethod
    def for_home(cls, home: Path) -> "RuntimePaths":
        return cls(
            legacy_state=home / ".local" / "share" / "mem0-arch",
            legacy_config=home / ".config" / "mem0-arch",
            target_state=home / ".local" / "share" / "mem0_0-arch",
            target_config=home / ".config" / "mem0_0-arch",
        )


SHADOW_ENV_UPDATES = {
    "HISTORY_DB_PATH": "/app/state/.mem0_0/history.db",
    "MEM0_DIR": "/app/state/.mem0_0",
    "MEM0_LMSTUDIO_COMPAT": "true",
    "MEM0_LMSTUDIO_REASONING_EFFORT": "none",
    "MEM0_0_DEPLOYMENT_MODE": "shadow",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "25432",
}


def _parse_env(path: Path) -> tuple[list[tuple[str | None, str]], dict[str, str]]:
    lines: list[tuple[str | None, str]] = []
    values: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationConflictError(f"required legacy configuration is unavailable: {path}") from exc
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append((None, raw_line))
            continue
        if "=" not in raw_line:
            raise MigrationConflictError(f"invalid environment entry at {path}:{line_number}")
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if not key or key in values:
            raise MigrationConflictError(f"duplicate or empty environment key at {path}:{line_number}")
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in key):
            raise MigrationConflictError(f"unsupported environment key at {path}:{line_number}")
        values[key] = value
        lines.append((key, raw_line))
    return lines, values


def _render_shadow_env(lines: list[tuple[str | None, str]], values: dict[str, str]) -> str:
    rendered = []
    seen = set()
    for key, raw_line in lines:
        if key in SHADOW_ENV_UPDATES:
            rendered.append(f"{key}={SHADOW_ENV_UPDATES[key]}")
            seen.add(key)
        else:
            rendered.append(raw_line)
    for key, value in sorted(SHADOW_ENV_UPDATES.items()):
        if key not in seen and key not in values:
            rendered.append(f"{key}={value}")
    return "\n".join(rendered) + "\n"


def _write_private(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_private(source: Path, target: Path) -> None:
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise MigrationConflictError(f"required legacy private file is unavailable: {source}") from exc
    _write_private(target, content)


def _require_private(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise MigrationConflictError(f"required legacy private file is unavailable: {path}") from exc
    if mode & 0o077:
        raise MigrationConflictError(f"legacy private file permissions are too broad: {path}")


def _preflight(paths: RuntimePaths, *, applying: bool) -> tuple[list[tuple[str | None, str]], dict[str, str]]:
    resolved = [
        paths.legacy_state.resolve(),
        paths.legacy_config.resolve(),
        paths.target_state.resolve(),
        paths.target_config.resolve(),
    ]
    if len(set(resolved)) != 4:
        raise MigrationConflictError("legacy and target paths must be distinct")
    source_env = paths.legacy_config / "runtime.env"
    source_bridge_key = paths.legacy_config / "bridge-api-key"
    source_history = paths.legacy_state / "history" / "history.db"
    _require_private(source_env)
    _require_private(source_bridge_key)
    if not source_history.is_file():
        raise MigrationConflictError(f"required legacy history database is unavailable: {source_history}")
    if applying:
        for target in (paths.target_state, paths.target_config):
            if target.exists():
                raise MigrationConflictError(f"target already exists; refusing to merge or overwrite: {target}")
    return _parse_env(source_env)


def prepare_shadow(paths: RuntimePaths, *, dry_run: bool = True) -> dict[str, Any]:
    """Prepare private empty shadow roots; database restoration is a separate gate."""

    lines, values = _preflight(paths, applying=not dry_run)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "planned" if dry_run else "prepared",
        "deployment": "mem0_0-arch-shadow",
        "copied_environment_keys": sorted(values),
        "history_source_inventory": sqlite_inventory(paths.legacy_state / "history" / "history.db"),
        "ports": {"api": 28889, "postgres": 25432},
        "source_unchanged": True,
    }
    if dry_run:
        return report

    paths.target_state.mkdir(mode=0o700, parents=True)
    paths.target_config.mkdir(mode=0o700, parents=True)
    for directory in (
        paths.target_state / "backups",
        paths.target_state / "receipts",
        paths.target_state / "runtime",
        paths.target_state / "runtime" / ".mem0_0",
    ):
        directory.mkdir(mode=0o700, parents=True)
        directory.chmod(0o700)
    paths.target_state.chmod(0o700)
    paths.target_config.chmod(0o700)
    _write_private(paths.target_config / "shadow.env", _render_shadow_env(lines, values).encode())
    _copy_private(paths.legacy_config / "bridge-api-key", paths.target_config / "bridge-api-key")
    age_key = paths.legacy_config / "backup.agekey"
    if age_key.exists():
        _require_private(age_key)
        _copy_private(age_key, paths.target_config / "backup.agekey")
        report["age_key_copied"] = True
    else:
        report["age_key_copied"] = False
    return report


def _table_digest(connection: sqlite3.Connection, table: str, order_columns: tuple[str, ...]) -> tuple[int, str]:
    available = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
    if not set(order_columns).issubset(available):
        raise MigrationConflictError(f"history database table {table} has an incompatible schema")
    quoted_order = ", ".join(f'"{column}"' for column in order_columns)
    quoted_columns = ", ".join(f'"{column}"' for column in available)
    rows = connection.execute(f'SELECT {quoted_columns} FROM "{table}" ORDER BY {quoted_order}')
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        canonical = [{"bytes_hex": value.hex()} if isinstance(value, bytes) else value for value in row]
        digest.update(json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def sqlite_inventory(path: Path) -> dict[str, Any]:
    """Return content-free counts and stable hashes of identity columns only."""

    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise MigrationConflictError(f"cannot open history database read-only: {path}") from exc
    try:
        history_rows, history_digest = _table_digest(connection, "history", ("id", "memory_id", "event", "is_deleted"))
        message_rows, message_digest = _table_digest(connection, "messages", ("id", "session_scope", "role"))
    except sqlite3.Error as exc:
        raise MigrationConflictError(f"cannot inventory history database: {path}") from exc
    finally:
        connection.close()
    return {
        "schema_version": 1,
        "tables": {
            "history": {"rows": history_rows, "content_sha256": history_digest},
            "messages": {"rows": message_rows, "content_sha256": message_digest},
        },
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare canonical mem0_0 Arch shadow state without overwriting data")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--apply", action="store_true", help="Create private shadow roots; default is a dry run")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = prepare_shadow(RuntimePaths.for_home(args.home), dry_run=not args.apply)
    if args.report:
        args.report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_json(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
