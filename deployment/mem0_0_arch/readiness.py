from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from deployment.mem0_0_arch.backup_restore import CommandRunner


class Runner(Protocol):
    def run(self, command: Sequence[str], **kwargs: Any) -> bytes: ...


def _private_file(path: Path) -> bool:
    try:
        return path.is_file() and not (path.stat().st_mode & 0o077)
    except OSError:
        return False


def _default_open(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(1024 * 1024)


def readiness_report(
    *,
    config_path: Path,
    history_path: Path,
    api_url: str,
    runner: Runner | None = None,
    opener: Callable[[urllib.request.Request, float], bytes] = _default_open,
) -> dict[str, Any]:
    runner = runner or CommandRunner()
    checks = {
        "config": "private" if _private_file(config_path) else "unavailable",
        "history": "private" if _private_file(history_path) else "unavailable",
        "postgres": "unavailable",
        "api": "unavailable",
    }
    try:
        runner.run(
            [
                "podman",
                "exec",
                "mem0_0-arch-shadow-postgres-1",
                "pg_isready",
                "-q",
                "-h",
                "127.0.0.1",
                "-p",
                "25432",
                "-U",
                "mem0",
            ]
        )
        checks["postgres"] = "ready"
    except Exception:
        pass
    try:
        payload = json.loads(opener(urllib.request.Request(f"{api_url.rstrip('/')}/openapi.json"), 5).decode())
        if isinstance(payload, dict) and str(payload.get("openapi", "")).startswith("3."):
            checks["api"] = "ready"
    except Exception:
        pass
    return {
        "schema_version": 1,
        "status": "ready" if all(value in {"ready", "private"} for value in checks.values()) else "degraded",
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check canonical mem0_0 shadow readiness without exposing content")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--api-url", default="http://127.0.0.1:28889")
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    state = args.home / ".local" / "share" / "mem0_0-arch"
    config = args.home / ".config" / "mem0_0-arch"
    deadline = time.monotonic() + max(args.wait_seconds, 0)
    while True:
        report = readiness_report(
            config_path=config / "shadow.env",
            history_path=state / "runtime" / ".mem0_0" / "history.db",
            api_url=args.api_url,
        )
        if report["status"] == "ready" or time.monotonic() >= deadline:
            break
        time.sleep(2)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
