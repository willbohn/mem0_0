from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from evaluation_overlay.harness import load_manifest, verify_model_receipts


@dataclass(frozen=True)
class ModelProxyConfig:
    api_key: str
    artifact_path: Path
    artifact_sha256: str
    launcher_sha256: str
    listen_host: str
    listen_port: int
    model_id: str
    receipt_sha256: str
    upstream: str


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return bool(host) and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_proxy_config(config: ModelProxyConfig) -> None:
    if not config.api_key:
        raise ValueError("a non-empty local API key is required")
    if not _is_loopback(config.listen_host):
        raise ValueError("model proxy must listen on a loopback address")
    if not isinstance(config.listen_port, int) or not 1 <= config.listen_port <= 65535:
        raise ValueError("model proxy port must be between 1 and 65535")
    if not config.model_id:
        raise ValueError("model ID is required")
    for value, label in (
        (config.artifact_sha256, "model artifact SHA-256"),
        (config.launcher_sha256, "launcher SHA-256"),
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    if len(config.receipt_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in config.receipt_sha256
    ):
        raise ValueError("model receipt fingerprint must be 64 lowercase hexadecimal characters")
    parsed = urllib.parse.urlsplit(config.upstream)
    if parsed.scheme != "http" or not _is_loopback(parsed.hostname):
        raise ValueError("model proxy upstream must be an HTTP loopback URL")
    if not parsed.port:
        raise ValueError("model proxy upstream must specify a port")


def build_models_payload(config: ModelProxyConfig) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": config.model_id,
                "object": "model",
                "owned_by": "mem0_0-local",
                "metadata": {"mem0_0_artifact_fingerprint": config.receipt_sha256},
            }
        ],
    }


def build_proxy_attestation(config: ModelProxyConfig) -> dict[str, str]:
    return {
        "artifact_sha256": config.artifact_sha256,
        "kind": "mem0_0_model_proxy_v1",
        "launcher_sha256": config.launcher_sha256,
        "model_id": config.model_id,
        "receipt_sha256": config.receipt_sha256,
    }


def build_llama_command(
    config: ModelProxyConfig,
    launcher: Path,
    api_key_file: Path,
    model_role: str,
) -> list[str]:
    parsed = urllib.parse.urlsplit(config.upstream)
    command = [
        str(launcher),
        "--model",
        str(config.artifact_path),
        "--alias",
        config.model_id,
        "--host",
        parsed.hostname or "127.0.0.1",
        "--port",
        str(parsed.port),
        "--api-key-file",
        str(api_key_file),
        "--parallel",
        "1",
        "--ctx-size",
        "8192" if model_role == "embedder" else "32768",
    ]
    if model_role == "embedder":
        command.extend(("--embedding", "--pooling", "mean"))
    return command


def _upstream_url(config: ModelProxyConfig, request_path: str) -> str:
    parsed = urllib.parse.urlsplit(request_path)
    base = config.upstream.rstrip("/")
    path = parsed.path
    if path.startswith("/v1/"):
        path = path[3:]
    elif path == "/v1":
        path = ""
    return f"{base}/{path.lstrip('/')}" + (f"?{parsed.query}" if parsed.query else "")


def _request_upstream(
    config: ModelProxyConfig, method: str, path: str, body: bytes | None = None
) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        _upstream_url(config, path),
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


def _verify_upstream_model(config: ModelProxyConfig) -> None:
    status, _, body = _request_upstream(config, "GET", "/v1/models")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("upstream model server returned invalid model metadata") from exc
    advertised = {
        str(item.get("id")) for item in (payload.get("data") or []) if isinstance(item, dict) and item.get("id")
    }
    if status != 200 or config.model_id not in advertised:
        raise RuntimeError(f"upstream model server does not advertise pinned model {config.model_id}")


def create_server(config: ModelProxyConfig) -> ThreadingHTTPServer:
    validate_proxy_config(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "mem0_0-model-proxy/1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            expected = f"Bearer {config.api_key}"
            return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return False

        def do_GET(self) -> None:
            if not self._require_auth():
                return
            if urllib.parse.urlsplit(self.path).path == "/v1/models":
                try:
                    _verify_upstream_model(config)
                except RuntimeError as exc:
                    self._send(503, "application/json", json.dumps({"error": str(exc)}).encode())
                    return
                self._send(200, "application/json", json.dumps(build_models_payload(config)).encode())
                return
            if urllib.parse.urlsplit(self.path).path == "/v1/mem0_0/attestation":
                self._send(200, "application/json", json.dumps(build_proxy_attestation(config)).encode())
                return
            status, content_type, body = _request_upstream(config, "GET", self.path)
            self._send(status, content_type, body)

        def do_POST(self) -> None:
            if not self._require_auth():
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            status, content_type, response = _request_upstream(config, "POST", self.path, body)
            self._send(status, content_type, response)

    return ThreadingHTTPServer((config.listen_host, config.listen_port), Handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Loopback attestation proxy for a pinned llama-server model")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("manifest.json"))
    parser.add_argument("--work-dir", type=Path, default=Path(".evaluation-overlay"))
    parser.add_argument("--model", choices=("llm", "embedder"), required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    return parser


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_launcher(manifest: dict[str, Any]) -> Path:
    pin = manifest["runtimes"]["llama_server"]
    resolved = shutil.which(pin["binary"])
    if not resolved:
        raise RuntimeError(f"pinned model launcher {pin['binary']} is not installed")
    launcher = Path(resolved).resolve()
    if _sha256_file(launcher) != pin["sha256"]:
        raise RuntimeError("installed llama-server binary does not match the pinned SHA-256")
    completed = subprocess.run([str(launcher), "--version"], check=True, capture_output=True, text=True)
    version_text = completed.stdout + completed.stderr
    if f"version: {pin['version']} " not in version_text:
        raise RuntimeError("installed llama-server version does not match the manifest")
    return launcher


def _wait_for_upstream(config: ModelProxyConfig, process: subprocess.Popen[bytes], timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"llama-server exited before readiness with status {process.returncode}")
        try:
            _verify_upstream_model(config)
            return
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError("llama-server did not become ready before timeout") from last_error


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("MEM0_BENCH_LOCAL_API_KEY", "")
    manifest = load_manifest(args.manifest.resolve())
    receipts = verify_model_receipts(manifest, args.work_dir.resolve())
    launcher = _verified_launcher(manifest)
    artifact = manifest["models"][args.model]["artifact"]
    artifact_path = (args.work_dir.resolve() / "models" / args.model / artifact["path"]).resolve()
    config = ModelProxyConfig(
        api_key=api_key,
        artifact_path=artifact_path,
        artifact_sha256=artifact["sha256"],
        launcher_sha256=manifest["runtimes"]["llama_server"]["sha256"],
        listen_host=args.listen_host,
        listen_port=args.port,
        model_id=manifest["models"][args.model]["id"],
        receipt_sha256=receipts[args.model]["receipt_sha256"],
        upstream=f"http://127.0.0.1:{args.upstream_port}/v1",
    )
    validate_proxy_config(config)
    runtime_dir = args.work_dir.resolve() / "runtime" / "models" / args.model
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "llama-server.log"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=runtime_dir, prefix=".api-key-", delete=False
    ) as key_file:
        key_file.write(api_key + "\n")
        key_file_path = Path(key_file.name)
    key_file_path.chmod(0o600)
    log = log_path.open("ab")
    process = subprocess.Popen(
        build_llama_command(config, launcher, key_file_path, args.model),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_upstream(config, process)
        key_file_path.unlink(missing_ok=True)
        server = create_server(config)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        key_file_path.unlink(missing_ok=True)
        if "server" in locals():
            server.server_close()
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
