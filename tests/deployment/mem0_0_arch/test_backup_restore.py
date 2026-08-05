import hashlib
import json
import os
import shutil
import sqlite3

import pytest

from deployment.mem0_0_arch.backup_restore import (
    BackupVerificationError,
    compare_inventories,
    postgres_inventory,
    restore_shadow,
    verify_backup_receipt,
    verify_legacy_sidecar,
)
from deployment.mem0_0_arch.migration import RuntimePaths, sqlite_inventory


class FakeRunner:
    def __init__(self, *outputs: bytes):
        self.outputs = list(outputs)
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(tuple(command))
        return self.outputs.pop(0)


def test_postgres_inventory_hashes_full_rows_without_returning_them():
    runner = FakeRunner(b"1\n", b'{"id":"id-a","memory":"private-a"}\n{"id":"id-b","memory":"private-b"}\n')

    inventory = postgres_inventory(runner, "legacy-postgres", 15432)

    assert inventory == {
        "rows": 2,
        "content_sha256": hashlib.sha256(
            b'{"id":"id-a","memory":"private-a"}\n{"id":"id-b","memory":"private-b"}\n'
        ).hexdigest(),
    }
    assert "id-a" not in json.dumps(inventory)
    assert runner.commands[0][0:3] == ("podman", "exec", "legacy-postgres")


def test_postgres_inventory_treats_a_fresh_database_without_memory_table_as_empty():
    runner = FakeRunner(b"0\n")

    inventory = postgres_inventory(runner, "shadow-postgres", 25432)

    assert inventory == {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()}
    assert len(runner.commands) == 1


def test_compare_inventories_fails_on_any_count_or_digest_drift():
    expected = {"rows": 2, "content_sha256": "a" * 64}

    assert compare_inventories(expected, dict(expected), label="postgres") is True
    with pytest.raises(BackupVerificationError, match="postgres inventory mismatch"):
        compare_inventories(expected, {"rows": 1, "content_sha256": "a" * 64}, label="postgres")


def test_backup_receipt_verification_detects_ciphertext_changes(tmp_path):
    postgres = tmp_path / "postgres.dump.age"
    sqlite = tmp_path / "history.sqlite.age"
    postgres.write_bytes(b"encrypted-postgres")
    sqlite.write_bytes(b"encrypted-sqlite")
    receipt = {
        "schema_version": 1,
        "source_revision": "a" * 40,
        "source_tree_sha256": "b" * 64,
        "cross_store_consistency": "not_proven",
        "artifacts": {
            "postgres": {"path": postgres.name, "sha256": hashlib.sha256(postgres.read_bytes()).hexdigest()},
            "sqlite": {"path": sqlite.name, "sha256": hashlib.sha256(sqlite.read_bytes()).hexdigest()},
        },
        "source": {
            "postgres": {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
            "sqlite": {
                "schema_version": 1,
                "tables": {
                    "history": {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
                    "messages": {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
                },
            },
        },
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert verify_backup_receipt(receipt_path)["schema_version"] == 1
    postgres.write_bytes(b"changed")
    with pytest.raises(BackupVerificationError, match="SHA-256"):
        verify_backup_receipt(receipt_path)


def test_backup_receipt_requires_exact_source_attestation(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"schema_version": 1}', encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="source attestation"):
        verify_backup_receipt(receipt)


def test_legacy_sidecar_is_accepted_only_for_the_exact_unchanged_ciphertext(tmp_path):
    artifact = tmp_path / "legacy.dump.age"
    artifact.write_bytes(b"legacy-encrypted-dump")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    sidecar = tmp_path / "legacy.dump.age.sha256"
    sidecar.write_text(f"{digest}  {artifact}\n", encoding="utf-8")

    assert verify_legacy_sidecar(artifact, sidecar) == digest
    sidecar.write_text(f"{digest}  {tmp_path / 'different.dump.age'}\n", encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="does not name"):
        verify_legacy_sidecar(artifact, sidecar)


def test_failed_postgres_reconciliation_never_commits_shadow_history(tmp_path, monkeypatch):
    paths = RuntimePaths(
        legacy_state=tmp_path / "legacy-state",
        legacy_config=tmp_path / "legacy-config",
        target_state=tmp_path / "target-state",
        target_config=tmp_path / "target-config",
    )
    target_runtime = paths.target_state / "runtime" / ".mem0_0"
    target_runtime.mkdir(parents=True)
    paths.target_config.mkdir()
    key = paths.target_config / "backup.agekey"
    key.write_text("synthetic-key", encoding="utf-8")
    os.chmod(key, 0o600)
    source_history = tmp_path / "source.sqlite"
    database = sqlite3.connect(source_history)
    database.execute(
        "CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, new_memory TEXT, event TEXT, "
        "created_at TEXT, updated_at TEXT, is_deleted INTEGER, actor_id TEXT, role TEXT)"
    )
    database.execute(
        "CREATE TABLE messages (id INTEGER, session_scope TEXT, role TEXT, content TEXT, name TEXT, created_at TEXT)"
    )
    database.commit()
    database.close()
    postgres_artifact = tmp_path / "postgres.age"
    sqlite_artifact = tmp_path / "sqlite.age"
    postgres_artifact.write_bytes(b"encrypted-postgres")
    sqlite_artifact.write_bytes(b"encrypted-sqlite")
    expected_postgres = {"rows": 1, "content_sha256": "a" * 64}
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_revision": "a" * 40,
                "source_tree_sha256": "b" * 64,
                "cross_store_consistency": "not_proven",
                "source": {"postgres": expected_postgres, "sqlite": sqlite_inventory(source_history)},
                "artifacts": {
                    "postgres": {
                        "path": postgres_artifact.name,
                        "sha256": hashlib.sha256(postgres_artifact.read_bytes()).hexdigest(),
                    },
                    "sqlite": {
                        "path": sqlite_artifact.name,
                        "sha256": hashlib.sha256(sqlite_artifact.read_bytes()).hexdigest(),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    inventories = iter(
        [
            {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()},
            {"rows": 1, "content_sha256": "c" * 64},
        ]
    )
    monkeypatch.setattr("deployment.mem0_0_arch.backup_restore.postgres_inventory", lambda *args: next(inventories))
    monkeypatch.setattr(
        "deployment.mem0_0_arch.backup_restore._restore_postgres_pipeline", lambda *args, **kwargs: None
    )

    class RestoreRunner:
        def run(self, command, **kwargs):
            shutil.copyfile(source_history, command[command.index("-o") + 1])
            return b""

    with pytest.raises(BackupVerificationError, match="PostgreSQL inventory mismatch"):
        restore_shadow(
            paths,
            receipt,
            shadow_postgres_container="mem0_0-arch-shadow-postgres-1",
            runner=RestoreRunner(),
        )

    assert not (target_runtime / "history.db").exists()
