from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from deployment.mem0_0_arch.migration import MigrationConflictError, RuntimePaths, sqlite_inventory
from deployment.mem0_0_arch.source_attestation import source_attestation


class BackupVerificationError(RuntimeError):
    """Raised when backup, restore, or content-free reconciliation cannot be proven."""


LEGACY_APP_WRITER_CONTAINER = "mem0-arch-mem0-1"


class Runner(Protocol):
    def run(self, command: Sequence[str], **kwargs: Any) -> bytes: ...

    def pipeline(self, commands: Sequence[Sequence[str]]) -> None: ...


class CommandRunner:
    def run(self, command: Sequence[str], **kwargs: Any) -> bytes:
        try:
            completed = subprocess.run(
                list(command),
                check=True,
                capture_output=True,
                input=kwargs.get("input"),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise BackupVerificationError(f"command failed without exposing its output: {command[0]}") from exc
        return completed.stdout

    def pipeline(self, commands: Sequence[Sequence[str]]) -> None:
        if len(commands) != 2:
            raise ValueError("only explicit two-process pipelines are supported")
        try:
            first = subprocess.Popen(list(commands[0]), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert first.stdout is not None
            second = subprocess.Popen(
                list(commands[1]), stdin=first.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            first.stdout.close()
            second.communicate()
            first.wait()
        except OSError as exc:
            raise BackupVerificationError("backup pipeline could not start") from exc
        if first.returncode or second.returncode:
            raise BackupVerificationError("backup pipeline failed without exposing provider output")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_private(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise BackupVerificationError(f"required private file is unavailable: {path}") from exc
    if mode & 0o077:
        raise BackupVerificationError(f"private file permissions are too broad: {path}")


def postgres_inventory(runner: Runner, container: str, port: int) -> dict[str, Any]:
    table_exists = runner.run(
        [
            "podman",
            "exec",
            container,
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "mem0",
            "-d",
            "postgres",
            "-At",
            "-c",
            "SELECT CASE WHEN to_regclass('public.memories') IS NULL THEN '0' ELSE '1' END;",
        ]
    ).strip()
    if table_exists == b"0":
        return {"rows": 0, "content_sha256": hashlib.sha256(b"").hexdigest()}
    if table_exists != b"1":
        raise BackupVerificationError("PostgreSQL returned an invalid memory-table preflight")
    output = runner.run(
        [
            "podman",
            "exec",
            container,
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "mem0",
            "-d",
            "postgres",
            "-At",
            "-c",
            "SELECT row_to_json(memory_row)::text FROM memories AS memory_row ORDER BY id::text;",
        ]
    )
    records = [line for line in output.splitlines() if line]
    canonical = b"".join(record + b"\n" for record in records)
    return {"rows": len(records), "content_sha256": hashlib.sha256(canonical).hexdigest()}


def compare_inventories(expected: dict[str, Any], actual: dict[str, Any], *, label: str) -> bool:
    if expected != actual:
        raise BackupVerificationError(f"{label} inventory mismatch")
    return True


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupVerificationError(f"backup receipt is unavailable or invalid: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise BackupVerificationError("backup receipt has an unsupported schema")
    return value


def _is_lower_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str) and len(value) == length and all(character in "0123456789abcdef" for character in value)
    )


def verify_backup_receipt(receipt_path: Path) -> dict[str, Any]:
    receipt = _load_receipt(receipt_path)
    if not _is_lower_hex(receipt.get("source_revision"), 40) or not _is_lower_hex(
        receipt.get("source_tree_sha256"), 64
    ):
        raise BackupVerificationError("backup receipt has no exact source attestation")
    consistency = receipt.get("cross_store_consistency")
    if consistency not in {
        "verified",
        "not_proven",
        "legacy_postgres_with_current_sqlite_unverified",
    }:
        raise BackupVerificationError("backup receipt has no cross-store consistency disposition")
    source = receipt.get("source")
    if not isinstance(source, dict) or set(source) != {"postgres", "sqlite"}:
        raise BackupVerificationError("backup receipt has no complete source inventories")
    postgres = source["postgres"]
    sqlite = source["sqlite"]
    if (
        not isinstance(postgres, dict)
        or not isinstance(postgres.get("rows"), int)
        or postgres["rows"] < 0
        or not _is_lower_hex(postgres.get("content_sha256"), 64)
        or not isinstance(sqlite, dict)
        or sqlite.get("schema_version") != 1
        or not isinstance(sqlite.get("tables"), dict)
        or set(sqlite["tables"]) != {"history", "messages"}
    ):
        raise BackupVerificationError("backup receipt contains invalid source inventories")
    for table in sqlite["tables"].values():
        if (
            not isinstance(table, dict)
            or not isinstance(table.get("rows"), int)
            or table["rows"] < 0
            or not _is_lower_hex(table.get("content_sha256"), 64)
        ):
            raise BackupVerificationError("backup receipt contains invalid SQLite inventory")
    if consistency == "verified":
        quiescence = receipt.get("quiescence")
        if (
            not isinstance(quiescence, dict)
            or set(quiescence)
            != {
                "method",
                "writer_container",
                "pause_succeeded",
                "unpause_succeeded",
                "pre_backup",
                "post_backup",
            }
            or quiescence.get("method") != "podman_pause"
            or quiescence.get("writer_container") != LEGACY_APP_WRITER_CONTAINER
            or quiescence.get("pause_succeeded") is not True
            or quiescence.get("unpause_succeeded") is not True
            or quiescence.get("pre_backup") != source
            or quiescence.get("post_backup") != source
        ):
            raise BackupVerificationError("verified backup receipt has no valid quiescence evidence")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"postgres", "sqlite"}:
        raise BackupVerificationError("backup receipt must describe PostgreSQL and SQLite artifacts")
    for name, artifact in artifacts.items():
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise BackupVerificationError(f"backup receipt has invalid {name} artifact metadata")
        path = (receipt_path.parent / artifact["path"]).resolve()
        if path.parent != receipt_path.parent.resolve():
            raise BackupVerificationError("backup receipt artifact path escapes its directory")
        try:
            digest = _sha256_file(path)
        except OSError as exc:
            raise BackupVerificationError(f"backup artifact is missing: {name}") from exc
        if digest != artifact.get("sha256"):
            raise BackupVerificationError(f"backup artifact failed SHA-256 verification: {name}")
    return receipt


def verify_legacy_sidecar(artifact: Path, sidecar: Path) -> str:
    try:
        parts = sidecar.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise BackupVerificationError(f"legacy SHA-256 sidecar is unavailable: {sidecar}") from exc
    if len(parts) != 2 or len(parts[0]) != 64 or any(character not in "0123456789abcdef" for character in parts[0]):
        raise BackupVerificationError("legacy SHA-256 sidecar has an invalid format")
    referenced = Path(parts[1]).expanduser().resolve()
    if referenced != artifact.resolve():
        raise BackupVerificationError("legacy SHA-256 sidecar does not name the selected artifact")
    try:
        actual = _sha256_file(artifact)
    except OSError as exc:
        raise BackupVerificationError(f"legacy encrypted backup is unavailable: {artifact}") from exc
    if actual != parts[0]:
        raise BackupVerificationError("legacy encrypted backup failed SHA-256 verification")
    return actual


def _sqlite_backup(source: Path, target: Path) -> None:
    try:
        source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
        target_connection = sqlite3.connect(target)
        with target_connection:
            source_connection.backup(target_connection)
    except sqlite3.Error as exc:
        raise BackupVerificationError("consistent SQLite backup failed") from exc
    finally:
        if "source_connection" in locals():
            source_connection.close()
        if "target_connection" in locals():
            target_connection.close()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _restore_postgres_pipeline(
    runner: Runner,
    *,
    encrypted_artifact: Path,
    age_key: Path,
    shadow_postgres_container: str,
) -> None:
    runner.pipeline(
        [
            ["age", "-d", "-i", str(age_key), str(encrypted_artifact)],
            [
                "podman",
                "exec",
                "-i",
                shadow_postgres_container,
                "pg_restore",
                "-h",
                "127.0.0.1",
                "-p",
                "25432",
                "-U",
                "mem0",
                "-d",
                "postgres",
                "--no-owner",
                "--clean",
                "--if-exists",
                "--exit-on-error",
            ],
        ]
    )


def create_encrypted_backup(
    paths: RuntimePaths,
    *,
    legacy_postgres_container: str,
    legacy_writer_container: str | None = None,
    source_repository: Path,
    runner: Runner | None = None,
    timestamp: str | None = None,
) -> Path:
    runner = runner or CommandRunner()
    if legacy_writer_container is not None and legacy_writer_container != LEGACY_APP_WRITER_CONTAINER:
        raise MigrationConflictError("quiescence target must be the exact legacy app writer container")
    source = source_attestation(source_repository)
    backup_dir = paths.target_state / "backups"
    if not backup_dir.is_dir():
        raise MigrationConflictError("prepare the canonical shadow runtime before backup")
    backup_dir.chmod(0o700)
    age_key = paths.target_config / "backup.agekey"
    _require_private(age_key)
    recipient = runner.run(["age-keygen", "-y", str(age_key)]).decode("utf-8").strip()
    if not recipient.startswith("age1"):
        raise BackupVerificationError("age key did not produce a valid recipient")

    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"mem0_0-migration-{stamp}"
    postgres_output = backup_dir / f"{prefix}.postgres.dump.age"
    sqlite_output = backup_dir / f"{prefix}.history.sqlite.age"
    receipt_path = backup_dir / f"{prefix}.receipt.json"
    if any(path.exists() for path in (postgres_output, sqlite_output, receipt_path)):
        raise MigrationConflictError("backup target already exists")

    source_sqlite = paths.legacy_state / "history" / "history.db"
    postgres_temporary = postgres_output.with_name(f".{postgres_output.name}.tmp")
    sqlite_temporary = sqlite_output.with_name(f".{sqlite_output.name}.tmp")
    with tempfile.NamedTemporaryFile(dir=backup_dir, prefix=".history.", suffix=".sqlite", delete=False) as handle:
        plaintext_sqlite = Path(handle.name)
    plaintext_sqlite.unlink()
    writer_paused = False
    post_inventory: dict[str, Any] | None = None
    try:
        if legacy_writer_container is not None:
            runner.run(["podman", "pause", legacy_writer_container])
            writer_paused = True
        source_inventory = {
            "postgres": postgres_inventory(runner, legacy_postgres_container, 15432),
            "sqlite": sqlite_inventory(source_sqlite),
        }
        runner.pipeline(
            [
                [
                    "podman",
                    "exec",
                    legacy_postgres_container,
                    "pg_dump",
                    "-h",
                    "127.0.0.1",
                    "-p",
                    "15432",
                    "-U",
                    "mem0",
                    "-d",
                    "postgres",
                    "--format=custom",
                    "--no-owner",
                ],
                ["age", "-r", recipient, "-o", str(postgres_temporary)],
            ]
        )
        _sqlite_backup(source_sqlite, plaintext_sqlite)
        runner.run(["age", "-r", recipient, "-o", str(sqlite_temporary), str(plaintext_sqlite)])
        for temporary in (postgres_temporary, sqlite_temporary):
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise BackupVerificationError("encryption did not create a non-empty artifact")
            temporary.chmod(0o600)
        os.replace(postgres_temporary, postgres_output)
        os.replace(sqlite_temporary, sqlite_output)
        if legacy_writer_container is not None:
            post_inventory = {
                "postgres": postgres_inventory(runner, legacy_postgres_container, 15432),
                "sqlite": sqlite_inventory(source_sqlite),
            }
            compare_inventories(source_inventory["postgres"], post_inventory["postgres"], label="PostgreSQL")
            compare_inventories(source_inventory["sqlite"], post_inventory["sqlite"], label="SQLite")
        if source_attestation(source_repository) != source:
            raise BackupVerificationError("source tree changed while backup evidence was being created")
    except Exception:
        postgres_output.unlink(missing_ok=True)
        sqlite_output.unlink(missing_ok=True)
        raise
    finally:
        plaintext_sqlite.unlink(missing_ok=True)
        postgres_temporary.unlink(missing_ok=True)
        sqlite_temporary.unlink(missing_ok=True)
        if writer_paused:
            try:
                runner.run(["podman", "unpause", legacy_writer_container])
            except Exception:
                postgres_output.unlink(missing_ok=True)
                sqlite_output.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                raise

    try:
        receipt = {
            "schema_version": 1,
            "created_at": stamp,
            "source_revision": source["revision"],
            "source_tree_sha256": source["source_tree_sha256"],
            "cross_store_consistency": "verified" if legacy_writer_container is not None else "not_proven",
            "recipient_sha256": hashlib.sha256(recipient.encode()).hexdigest(),
            "source": source_inventory,
            "artifacts": {
                "postgres": {"path": postgres_output.name, "sha256": _sha256_file(postgres_output)},
                "sqlite": {"path": sqlite_output.name, "sha256": _sha256_file(sqlite_output)},
            },
        }
        if legacy_writer_container is not None:
            receipt["quiescence"] = {
                "method": "podman_pause",
                "writer_container": legacy_writer_container,
                "pause_succeeded": True,
                "unpause_succeeded": True,
                "pre_backup": source_inventory,
                "post_backup": post_inventory,
            }
        _atomic_json(receipt_path, receipt)
        verify_backup_receipt(receipt_path)
    except Exception:
        postgres_output.unlink(missing_ok=True)
        sqlite_output.unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)
        raise
    return receipt_path


def restore_shadow(
    paths: RuntimePaths,
    receipt_path: Path,
    *,
    shadow_postgres_container: str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    runner = runner or CommandRunner()
    if shadow_postgres_container != "mem0_0-arch-shadow-postgres-1":
        raise MigrationConflictError("restore target must be the canonical isolated shadow container")
    receipt = verify_backup_receipt(receipt_path)
    target_history = paths.target_state / "runtime" / ".mem0_0" / "history.db"
    if target_history.exists():
        raise MigrationConflictError("shadow history already exists; refusing to overwrite")
    empty_postgres = postgres_inventory(runner, shadow_postgres_container, 25432)
    if empty_postgres["rows"] != 0:
        raise MigrationConflictError("shadow PostgreSQL is not empty; refusing to merge restored data")
    age_key = paths.target_config / "backup.agekey"
    _require_private(age_key)
    postgres_artifact = receipt_path.parent / receipt["artifacts"]["postgres"]["path"]
    sqlite_artifact = receipt_path.parent / receipt["artifacts"]["sqlite"]["path"]
    _restore_postgres_pipeline(
        runner,
        encrypted_artifact=postgres_artifact,
        age_key=age_key,
        shadow_postgres_container=shadow_postgres_container,
    )
    temporary = target_history.with_name(f".{target_history.name}.{os.getpid()}.restore")
    try:
        runner.run(["age", "-d", "-i", str(age_key), "-o", str(temporary), str(sqlite_artifact)])
        compare_inventories(receipt["source"]["sqlite"], sqlite_inventory(temporary), label="SQLite")
        restored_postgres = postgres_inventory(runner, shadow_postgres_container, 25432)
        compare_inventories(receipt["source"]["postgres"], restored_postgres, label="PostgreSQL")
        temporary.chmod(0o600)
        os.replace(temporary, target_history)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema_version": 1,
        "status": "restored",
        "postgres": restored_postgres,
        "sqlite": sqlite_inventory(target_history),
        "legacy_state_retained": True,
    }


def reconcile_shadow(
    paths: RuntimePaths,
    receipt_path: Path,
    *,
    shadow_postgres_container: str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Prove that existing canonical stores match one quiesced legacy receipt without mutating either store."""

    runner = runner or CommandRunner()
    if shadow_postgres_container != "mem0_0-arch-shadow-postgres-1":
        raise MigrationConflictError("reconciliation target must be the canonical isolated shadow container")
    receipt = verify_backup_receipt(receipt_path)
    if receipt["cross_store_consistency"] != "verified":
        raise BackupVerificationError("shadow reconciliation requires a quiesced verified source receipt")
    target_history = paths.target_state / "runtime" / ".mem0_0" / "history.db"
    if not target_history.is_file():
        raise MigrationConflictError("shadow history is unavailable for reconciliation")
    postgres = postgres_inventory(runner, shadow_postgres_container, 25432)
    sqlite = sqlite_inventory(target_history)
    compare_inventories(receipt["source"]["postgres"], postgres, label="PostgreSQL")
    compare_inventories(receipt["source"]["sqlite"], sqlite, label="SQLite")
    return {
        "schema_version": 1,
        "status": "reconciled",
        "cross_store_consistency": "verified",
        "postgres": postgres,
        "sqlite": sqlite,
        "legacy_state_retained": True,
    }


def adopt_legacy_backup(
    paths: RuntimePaths,
    legacy_artifact: Path,
    legacy_sidecar: Path,
    *,
    shadow_postgres_container: str,
    source_repository: Path,
    runner: Runner | None = None,
    timestamp: str | None = None,
) -> Path:
    """Restore a verified legacy encrypted dump and emit a canonical two-store receipt."""

    runner = runner or CommandRunner()
    if shadow_postgres_container != "mem0_0-arch-shadow-postgres-1":
        raise MigrationConflictError("adoption target must be the canonical isolated shadow container")
    source = source_attestation(source_repository)
    verify_legacy_sidecar(legacy_artifact, legacy_sidecar)
    backup_dir = paths.target_state / "backups"
    target_history = paths.target_state / "runtime" / ".mem0_0" / "history.db"
    if not backup_dir.is_dir() or target_history.exists():
        raise MigrationConflictError("prepared empty canonical state is required for legacy backup adoption")
    empty_postgres = postgres_inventory(runner, shadow_postgres_container, 25432)
    if empty_postgres["rows"] != 0:
        raise MigrationConflictError("shadow PostgreSQL is not empty; refusing legacy backup adoption")
    age_key = paths.target_config / "backup.agekey"
    _require_private(age_key)
    _restore_postgres_pipeline(
        runner,
        encrypted_artifact=legacy_artifact,
        age_key=age_key,
        shadow_postgres_container=shadow_postgres_container,
    )
    postgres_source = postgres_inventory(runner, shadow_postgres_container, 25432)
    sqlite_source_path = paths.legacy_state / "history" / "history.db"
    sqlite_source = sqlite_inventory(sqlite_source_path)
    recipient = runner.run(["age-keygen", "-y", str(age_key)]).decode().strip()
    if not recipient.startswith("age1"):
        raise BackupVerificationError("age key did not produce a valid recipient")

    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"mem0_0-adopted-{stamp}"
    postgres_output = backup_dir / f"{prefix}.postgres.dump.age"
    sqlite_output = backup_dir / f"{prefix}.history.sqlite.age"
    receipt_path = backup_dir / f"{prefix}.receipt.json"
    if any(path.exists() for path in (postgres_output, sqlite_output, receipt_path)):
        raise MigrationConflictError("canonical adoption artifact already exists")
    postgres_temporary = postgres_output.with_name(f".{postgres_output.name}.tmp")
    sqlite_temporary = sqlite_output.with_name(f".{sqlite_output.name}.tmp")
    with tempfile.NamedTemporaryFile(dir=backup_dir, prefix=".history.", suffix=".sqlite", delete=False) as handle:
        plaintext_sqlite = Path(handle.name)
    plaintext_sqlite.unlink()
    restored_sqlite = target_history.with_name(f".{target_history.name}.{os.getpid()}.restore")
    try:
        shutil.copyfile(legacy_artifact, postgres_temporary)
        _sqlite_backup(sqlite_source_path, plaintext_sqlite)
        runner.run(["age", "-r", recipient, "-o", str(sqlite_temporary), str(plaintext_sqlite)])
        if not sqlite_temporary.is_file() or sqlite_temporary.stat().st_size == 0:
            raise BackupVerificationError("SQLite encryption did not create a non-empty artifact")
        for temporary in (postgres_temporary, sqlite_temporary):
            temporary.chmod(0o600)
        os.replace(postgres_temporary, postgres_output)
        os.replace(sqlite_temporary, sqlite_output)
        runner.run(["age", "-d", "-i", str(age_key), "-o", str(restored_sqlite), str(sqlite_output)])
        compare_inventories(sqlite_source, sqlite_inventory(restored_sqlite), label="SQLite")
        if source_attestation(source_repository) != source:
            raise BackupVerificationError("source tree changed while adoption evidence was being created")
        restored_sqlite.chmod(0o600)
        os.replace(restored_sqlite, target_history)
    except Exception:
        postgres_output.unlink(missing_ok=True)
        sqlite_output.unlink(missing_ok=True)
        raise
    finally:
        plaintext_sqlite.unlink(missing_ok=True)
        postgres_temporary.unlink(missing_ok=True)
        sqlite_temporary.unlink(missing_ok=True)
        restored_sqlite.unlink(missing_ok=True)

    receipt = {
        "schema_version": 1,
        "created_at": stamp,
        "source_revision": source["revision"],
        "source_tree_sha256": source["source_tree_sha256"],
        "cross_store_consistency": "legacy_postgres_with_current_sqlite_unverified",
        "adopted_legacy_artifact_sha256": _sha256_file(legacy_artifact),
        "recipient_sha256": hashlib.sha256(recipient.encode()).hexdigest(),
        "source": {"postgres": postgres_source, "sqlite": sqlite_source},
        "artifacts": {
            "postgres": {"path": postgres_output.name, "sha256": _sha256_file(postgres_output)},
            "sqlite": {"path": sqlite_output.name, "sha256": _sha256_file(sqlite_output)},
        },
    }
    _atomic_json(receipt_path, receipt)
    verify_backup_receipt(receipt_path)
    return receipt_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Encrypt or restore canonical mem0_0 migration backups")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--home", type=Path, default=Path.home())
    backup.add_argument("--legacy-container", default="mem0-arch-postgres-1")
    backup.add_argument(
        "--quiesce-legacy-writer",
        metavar="CONTAINER",
        help=f"Pause the exact legacy app writer ({LEGACY_APP_WRITER_CONTAINER}) while proving both stores stable",
    )
    backup.add_argument("--source-repository", type=Path, required=True)
    restore = subparsers.add_parser("restore-shadow")
    restore.add_argument("receipt", type=Path)
    restore.add_argument("--home", type=Path, default=Path.home())
    restore.add_argument("--shadow-container", default="mem0_0-arch-shadow-postgres-1")
    reconcile = subparsers.add_parser("reconcile-shadow")
    reconcile.add_argument("receipt", type=Path)
    reconcile.add_argument("--home", type=Path, default=Path.home())
    reconcile.add_argument("--shadow-container", default="mem0_0-arch-shadow-postgres-1")
    adopt = subparsers.add_parser("adopt-legacy-backup")
    adopt.add_argument("artifact", type=Path)
    adopt.add_argument("sidecar", type=Path)
    adopt.add_argument("--home", type=Path, default=Path.home())
    adopt.add_argument("--shadow-container", default="mem0_0-arch-shadow-postgres-1")
    adopt.add_argument("--source-repository", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = RuntimePaths.for_home(args.home)
    if args.command == "backup":
        receipt = create_encrypted_backup(
            paths,
            legacy_postgres_container=args.legacy_container,
            legacy_writer_container=args.quiesce_legacy_writer,
            source_repository=args.source_repository,
        )
        print(json.dumps({"status": "verified", "receipt": str(receipt)}, sort_keys=True))
    elif args.command == "restore-shadow":
        print(
            json.dumps(
                restore_shadow(paths, args.receipt, shadow_postgres_container=args.shadow_container),
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "reconcile-shadow":
        print(
            json.dumps(
                reconcile_shadow(paths, args.receipt, shadow_postgres_container=args.shadow_container),
                indent=2,
                sort_keys=True,
            )
        )
    else:
        receipt = adopt_legacy_backup(
            paths,
            args.artifact,
            args.sidecar,
            shadow_postgres_container=args.shadow_container,
            source_repository=args.source_repository,
        )
        print(json.dumps({"status": "adopted", "receipt": str(receipt)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
