from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from evaluation_overlay.harness import load_manifest, verify_model_receipts


@dataclass(frozen=True)
class ModelProxyConfig:
    api_key: str
    artifact_path: Path
    artifact_sha256: str
    device: str
    enable_thinking: bool
    gpu_layers: str
    launcher_sha256: str
    listen_host: str
    listen_port: int
    model_id: str
    offloaded_layers: int | None
    receipt_sha256: str
    resolved_device: str | None
    total_layers: int | None
    upstream: str
    batch_size: int = 2048
    ubatch_size: int = 512


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
    if not config.device or config.gpu_layers != "all":
        raise ValueError("an explicit device with all GPU layers is required")
    if config.enable_thinking is not False:
        raise ValueError("benchmark inference must explicitly disable thinking")
    if config.batch_size <= 0 or config.ubatch_size <= 0 or config.ubatch_size > config.batch_size:
        raise ValueError("model proxy batch sizes must be positive and physical must not exceed logical")
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


def build_proxy_attestation(config: ModelProxyConfig) -> dict[str, Any]:
    validate_runtime_placement(config)
    return {
        "artifact_sha256": config.artifact_sha256,
        "backend_verified": True,
        "batch_size": config.batch_size,
        "device": config.device,
        "enable_thinking": config.enable_thinking,
        "gpu_layers": config.gpu_layers,
        "kind": "mem0_0_model_proxy_v1",
        "launcher_sha256": config.launcher_sha256,
        "model_id": config.model_id,
        "offloaded_layers": config.offloaded_layers,
        "receipt_sha256": config.receipt_sha256,
        "resolved_device": config.resolved_device,
        "total_layers": config.total_layers,
        "ubatch_size": config.ubatch_size,
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
        "--device",
        config.device,
        "--gpu-layers",
        config.gpu_layers,
        "--verbosity",
        "4",
        "--parallel",
        "1",
        "--ctx-size",
        "8192" if model_role == "embedder" else "32768",
        "--batch-size",
        str(config.batch_size),
        "--ubatch-size",
        str(config.ubatch_size),
    ]
    if model_role == "embedder":
        command.extend(("--embedding", "--pooling", "mean"))
    return command


def parse_runtime_placement(config: ModelProxyConfig, log_text: str) -> dict[str, Any]:
    devices = re.findall(r"using device ([^\s(]+)\s*\(", log_text)
    offloads = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", log_text)
    if not devices or devices[-1] != config.device:
        raise RuntimeError(f"llama-server did not confirm requested device {config.device}")
    if not offloads:
        raise RuntimeError("llama-server did not report resolved layer offload placement")
    offloaded_layers, total_layers = (int(value) for value in offloads[-1])
    if offloaded_layers <= 0 or offloaded_layers != total_layers:
        raise RuntimeError(
            f"llama-server only offloaded {offloaded_layers}/{total_layers} model layers to {config.device}"
        )
    return {
        "offloaded_layers": offloaded_layers,
        "resolved_device": devices[-1],
        "total_layers": total_layers,
    }


def wait_for_runtime_placement(
    config: ModelProxyConfig,
    process: subprocess.Popen[bytes],
    log_path: Path,
    timeout: float = 30,
) -> dict[str, Any]:
    """Wait for the launcher log to contain complete placement evidence.

    The model endpoint can become ready before the launcher has flushed its
    startup diagnostics. Placement attestation therefore has its own bounded
    wait instead of racing the first successful ``/models`` response.
    """
    deadline = time.monotonic() + timeout
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"llama-server exited before placement attestation with status {process.returncode}")
        try:
            return parse_runtime_placement(config, log_path.read_text(encoding="utf-8", errors="replace"))
        except RuntimeError as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError("llama-server did not report complete runtime placement before timeout") from last_error


def validate_runtime_placement(config: ModelProxyConfig) -> None:
    if (
        config.resolved_device != config.device
        or not isinstance(config.offloaded_layers, int)
        or not isinstance(config.total_layers, int)
        or config.offloaded_layers <= 0
        or config.offloaded_layers != config.total_layers
    ):
        raise ValueError("model proxy requires verified complete layer placement on the requested device")


def _upstream_url(config: ModelProxyConfig, request_path: str) -> str:
    parsed = urllib.parse.urlsplit(request_path)
    base = config.upstream.rstrip("/")
    path = parsed.path
    if path.startswith("/v1/"):
        path = path[3:]
    elif path == "/v1":
        path = ""
    return f"{base}/{path.lstrip('/')}" + (f"?{parsed.query}" if parsed.query else "")


def prepare_upstream_body(config: ModelProxyConfig, path: str, body: bytes | None) -> bytes | None:
    if body is None or urllib.parse.urlsplit(path).path != "/v1/chat/completions":
        return body
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("chat completion request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("chat completion request body must be a JSON object")
    template_kwargs = payload.get("chat_template_kwargs")
    if template_kwargs is None:
        template_kwargs = {}
    elif not isinstance(template_kwargs, dict):
        raise ValueError("chat_template_kwargs must be a JSON object")
    payload["chat_template_kwargs"] = {**template_kwargs, "enable_thinking": config.enable_thinking}
    return json.dumps(payload, separators=(",", ":")).encode()


def _request_upstream(
    config: ModelProxyConfig, method: str, path: str, body: bytes | None = None
) -> tuple[int, str, bytes]:
    body = prepare_upstream_body(config, path, body)
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
    validate_runtime_placement(config)

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
            try:
                status, content_type, response = _request_upstream(config, "POST", self.path, body)
            except ValueError as exc:
                self._send(400, "application/json", json.dumps({"error": str(exc)}).encode())
                return
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
    batch_sizes = manifest["runtimes"]["llama_server"]["batch_sizes"][args.model]
    artifact_path = (args.work_dir.resolve() / "models" / args.model / artifact["path"]).resolve()
    config = ModelProxyConfig(
        api_key=api_key,
        artifact_path=artifact_path,
        artifact_sha256=artifact["sha256"],
        device=manifest["runtimes"]["llama_server"]["device"],
        enable_thinking=manifest["runtimes"]["llama_server"]["enable_thinking"],
        gpu_layers=manifest["runtimes"]["llama_server"]["gpu_layers"],
        launcher_sha256=manifest["runtimes"]["llama_server"]["sha256"],
        listen_host=args.listen_host,
        listen_port=args.port,
        model_id=manifest["models"][args.model]["id"],
        offloaded_layers=None,
        receipt_sha256=receipts[args.model]["receipt_sha256"],
        resolved_device=None,
        total_layers=None,
        upstream=f"http://127.0.0.1:{args.upstream_port}/v1",
        batch_size=batch_sizes["logical"],
        ubatch_size=batch_sizes["physical"],
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
    log = log_path.open("wb")
    process = subprocess.Popen(
        build_llama_command(config, launcher, key_file_path, args.model),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_upstream(config, process)
        placement = wait_for_runtime_placement(config, process, log_path)
        config = replace(config, **placement)
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
