import json
import os
import sqlite3
from pathlib import Path

import pytest

from deployment.mem0_0_arch.migration import (
    MigrationConflictError,
    RuntimePaths,
    prepare_shadow,
    sqlite_inventory,
)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        legacy_state=tmp_path / "legacy-state",
        legacy_config=tmp_path / "legacy-config",
        target_state=tmp_path / "target-state",
        target_config=tmp_path / "target-config",
    )


def _seed_legacy(paths: RuntimePaths) -> None:
    (paths.legacy_state / "history").mkdir(parents=True)
    paths.legacy_config.mkdir(parents=True)
    (paths.legacy_config / "runtime.env").write_text(
        "POSTGRES_HOST=127.0.0.1\n"
        "POSTGRES_PORT=15432\n"
        "POSTGRES_PASSWORD=synthetic-secret\n"
        "ADMIN_API_KEY=synthetic-admin\n",
        encoding="utf-8",
    )
    (paths.legacy_config / "bridge-api-key").write_text("synthetic-admin\n", encoding="utf-8")
    os.chmod(paths.legacy_config / "runtime.env", 0o600)
    os.chmod(paths.legacy_config / "bridge-api-key", 0o600)
    database = sqlite3.connect(paths.legacy_state / "history" / "history.db")
    database.execute(
        "CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, new_memory TEXT, event TEXT, "
        "created_at TEXT, updated_at TEXT, is_deleted INTEGER, actor_id TEXT, role TEXT)"
    )
    database.execute(
        "CREATE TABLE messages (id INTEGER, session_scope TEXT, role TEXT, content TEXT, name TEXT, created_at TEXT)"
    )
    database.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("h1", "m1", "private old", "private new", "ADD", "now", None, 0, "a", "user"),
    )
    database.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        (1, "scope", "user", "private message", None, "now"),
    )
    database.commit()
    database.close()


def test_prepare_shadow_dry_run_is_read_only_and_redacts_values(tmp_path):
    paths = _runtime_paths(tmp_path)
    _seed_legacy(paths)

    report = prepare_shadow(paths, dry_run=True)

    assert report["status"] == "planned"
    assert report["copied_environment_keys"] == [
        "ADMIN_API_KEY",
        "POSTGRES_HOST",
        "POSTGRES_PASSWORD",
        "POSTGRES_PORT",
    ]
    assert "synthetic-secret" not in json.dumps(report)
    assert not paths.target_state.exists()
    assert not paths.target_config.exists()


def test_prepare_shadow_creates_private_isolated_runtime_without_touching_source(tmp_path):
    paths = _runtime_paths(tmp_path)
    _seed_legacy(paths)
    source_env = (paths.legacy_config / "runtime.env").read_bytes()

    report = prepare_shadow(paths, dry_run=False)

    target_env = paths.target_config / "shadow.env"
    assert report["status"] == "prepared"
    assert oct(target_env.stat().st_mode & 0o777) == "0o600"
    assert oct(paths.target_state.stat().st_mode & 0o777) == "0o700"
    rendered = target_env.read_text(encoding="utf-8")
    assert "POSTGRES_PORT=25432" in rendered
    assert "MEM0_DIR=/app/state/.mem0_0" in rendered
    assert "HISTORY_DB_PATH=/app/state/.mem0_0/history.db" in rendered
    assert "MEM0_LMSTUDIO_COMPAT=true" in rendered
    assert "MEM0_LMSTUDIO_REASONING_EFFORT=none" in rendered
    assert "MEM0_LMSTUDIO_REQUEST_TIMEOUT_SECONDS=300" in rendered
    assert "MEM0_LMSTUDIO_MAX_TOKENS=256" in rendered
    assert "MEM0_LMSTUDIO_COMPACT_PROMPT=true" in rendered
    assert "synthetic-secret" in rendered
    assert (paths.legacy_config / "runtime.env").read_bytes() == source_env
    assert not (paths.target_state / "runtime" / ".mem0_0" / "history.db").exists()


def test_prepare_shadow_fails_closed_when_target_exists(tmp_path):
    paths = _runtime_paths(tmp_path)
    _seed_legacy(paths)
    paths.target_state.mkdir()

    with pytest.raises(MigrationConflictError, match="already exists"):
        prepare_shadow(paths, dry_run=False)


def test_sqlite_inventory_reports_only_counts_and_full_row_digests(tmp_path):
    paths = _runtime_paths(tmp_path)
    _seed_legacy(paths)
    source = paths.legacy_state / "history" / "history.db"

    inventory = sqlite_inventory(source)

    assert inventory["tables"] == {
        "history": {"rows": 1, "content_sha256": inventory["tables"]["history"]["content_sha256"]},
        "messages": {"rows": 1, "content_sha256": inventory["tables"]["messages"]["content_sha256"]},
    }
    serialized = json.dumps(inventory)
    assert "private old" not in serialized
    assert "private new" not in serialized
    assert "private message" not in serialized
    assert len(inventory["tables"]["history"]["content_sha256"]) == 64

    database = sqlite3.connect(source)
    database.execute("UPDATE history SET new_memory = ? WHERE id = ?", ("changed private body", "h1"))
    database.commit()
    database.close()

    changed = sqlite_inventory(source)
    assert changed["tables"]["history"]["content_sha256"] != inventory["tables"]["history"]["content_sha256"]
