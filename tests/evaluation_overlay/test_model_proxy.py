import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from evaluation_overlay.model_proxy import (
    ModelProxyConfig,
    build_llama_command,
    build_models_payload,
    build_proxy_attestation,
    parse_runtime_placement,
    prepare_upstream_body,
    wait_for_runtime_placement,
    validate_proxy_config,
)


def test_models_payload_attests_exact_receipt_fingerprint():
    fingerprint = hashlib.sha256(b"receipt").hexdigest()
    config = ModelProxyConfig(
        api_key="local-key",
        artifact_path=Path("/tmp/model.gguf"),
        artifact_sha256="b" * 64,
        device="Vulkan0",
        enable_thinking=False,
        gpu_layers="all",
        launcher_sha256="c" * 64,
        listen_host="127.0.0.1",
        listen_port=18000,
        model_id="example/model",
        offloaded_layers=25,
        receipt_sha256=fingerprint,
        resolved_device="Vulkan0",
        total_layers=25,
        upstream="http://127.0.0.1:18100/v1",
    )

    payload = build_models_payload(config)

    assert payload == {
        "object": "list",
        "data": [
            {
                "id": "example/model",
                "object": "model",
                "owned_by": "mem0_0-local",
                "metadata": {"mem0_0_artifact_fingerprint": fingerprint},
            }
        ],
    }
    json.dumps(payload)
    assert build_proxy_attestation(config) == {
        "artifact_sha256": "b" * 64,
        "backend_verified": True,
        "batch_size": 2048,
        "device": "Vulkan0",
        "enable_thinking": False,
        "gpu_layers": "all",
        "kind": "mem0_0_model_proxy_v1",
        "launcher_sha256": "c" * 64,
        "model_id": "example/model",
        "offloaded_layers": 25,
        "receipt_sha256": fingerprint,
        "resolved_device": "Vulkan0",
        "total_layers": 25,
        "ubatch_size": 512,
    }


def test_llama_command_binds_exact_artifact_alias_and_role(tmp_path):
    key_file = tmp_path / "api-key"
    llm = ModelProxyConfig(
        api_key="local-key",
        artifact_path=tmp_path / "model.gguf",
        artifact_sha256="b" * 64,
        device="Vulkan0",
        enable_thinking=False,
        gpu_layers="all",
        launcher_sha256="c" * 64,
        listen_host="127.0.0.1",
        listen_port=18000,
        model_id="example/model",
        offloaded_layers=25,
        receipt_sha256="a" * 64,
        resolved_device="Vulkan0",
        total_layers=25,
        upstream="http://127.0.0.1:18100/v1",
    )
    embedder = ModelProxyConfig(
        **{
            **llm.__dict__,
            "model_id": "example/embedder",
            "batch_size": 8192,
            "ubatch_size": 8192,
        }
    )

    llm_command = build_llama_command(llm, Path("/usr/bin/llama-server"), key_file, "llm")
    embedder_command = build_llama_command(embedder, Path("/usr/bin/llama-server"), key_file, "embedder")

    assert llm_command[llm_command.index("--model") + 1] == str(llm.artifact_path)
    assert llm_command[llm_command.index("--alias") + 1] == llm.model_id
    assert llm_command[llm_command.index("--device") + 1] == "Vulkan0"
    assert llm_command[llm_command.index("--gpu-layers") + 1] == "all"
    assert llm_command[llm_command.index("--verbosity") + 1] == "4"
    assert "--embedding" not in llm_command
    assert "--embedding" in embedder_command
    assert embedder_command[embedder_command.index("--pooling") + 1] == "mean"
    assert embedder_command[embedder_command.index("--batch-size") + 1] == "8192"
    assert embedder_command[embedder_command.index("--ubatch-size") + 1] == "8192"


def test_chat_completion_requests_disable_thinking_deterministically():
    config = ModelProxyConfig(
        api_key="local-key",
        artifact_path=Path("/tmp/model.gguf"),
        artifact_sha256="b" * 64,
        device="Vulkan0",
        enable_thinking=False,
        gpu_layers="all",
        launcher_sha256="c" * 64,
        listen_host="127.0.0.1",
        listen_port=18000,
        model_id="example/model",
        offloaded_layers=25,
        receipt_sha256="a" * 64,
        resolved_device="Vulkan0",
        total_layers=25,
        upstream="http://127.0.0.1:18100/v1",
    )
    body = json.dumps({"messages": [], "chat_template_kwargs": {"enable_thinking": True, "temperature": 0}}).encode()

    prepared = json.loads(prepare_upstream_body(config, "/v1/chat/completions", body))

    assert prepared["chat_template_kwargs"] == {"enable_thinking": False, "temperature": 0}
    assert prepare_upstream_body(config, "/v1/embeddings", body) == body


def test_runtime_placement_uses_resolved_device_and_complete_offload_evidence():
    values = {
        "api_key": "local-key",
        "artifact_path": Path("/tmp/model.gguf"),
        "artifact_sha256": "b" * 64,
        "device": "Vulkan0",
        "enable_thinking": False,
        "gpu_layers": "all",
        "launcher_sha256": "c" * 64,
        "listen_host": "127.0.0.1",
        "listen_port": 18000,
        "model_id": "example/model",
        "offloaded_layers": None,
        "receipt_sha256": "a" * 64,
        "resolved_device": None,
        "total_layers": None,
        "upstream": "http://127.0.0.1:18100/v1",
    }
    config = ModelProxyConfig(**values)

    placement = parse_runtime_placement(
        config,
        "using device Vulkan0 (Example GPU) - 1024 MiB free\nload_tensors: offloaded 25/25 layers to GPU\n",
    )

    assert placement == {"offloaded_layers": 25, "resolved_device": "Vulkan0", "total_layers": 25}
    with pytest.raises(RuntimeError, match="only offloaded"):
        parse_runtime_placement(
            config,
            "using device Vulkan0 (Example GPU) - 1024 MiB free\nload_tensors: offloaded 24/25 layers to GPU\n",
        )


def test_runtime_placement_waits_for_launcher_log_after_endpoint_readiness(tmp_path):
    config = ModelProxyConfig(
        api_key="local-key",
        artifact_path=Path("/tmp/model.gguf"),
        artifact_sha256="b" * 64,
        device="Vulkan0",
        enable_thinking=False,
        gpu_layers="all",
        launcher_sha256="c" * 64,
        listen_host="127.0.0.1",
        listen_port=18000,
        model_id="example/model",
        offloaded_layers=None,
        receipt_sha256="a" * 64,
        resolved_device=None,
        total_layers=None,
        upstream="http://127.0.0.1:18100/v1",
    )
    log_path = tmp_path / "llama-server.log"
    log_path.write_text("")

    class RunningProcess:
        returncode = None

        @staticmethod
        def poll():
            return None

    def write_placement_evidence():
        time.sleep(0.05)
        log_path.write_text(
            "using device Vulkan0 (Example GPU) - 1024 MiB free\n"
            "load_tensors: offloaded 25/25 layers to GPU\n"
        )

    writer = threading.Thread(target=write_placement_evidence)
    writer.start()
    try:
        placement = wait_for_runtime_placement(config, RunningProcess(), log_path, timeout=1)
    finally:
        writer.join()

    assert placement == {"offloaded_layers": 25, "resolved_device": "Vulkan0", "total_layers": 25}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("listen_host", "0.0.0.0", "loopback"),
        ("upstream", "https://models.example.com/v1", "loopback"),
        ("api_key", "", "API key"),
        ("enable_thinking", True, "disable thinking"),
        ("receipt_sha256", "bad", "receipt fingerprint"),
    ],
)
def test_proxy_config_fails_closed(field, value, message):
    values = {
        "api_key": "local-key",
        "artifact_path": Path("/tmp/model.gguf"),
        "artifact_sha256": "b" * 64,
        "device": "Vulkan0",
        "enable_thinking": False,
        "gpu_layers": "all",
        "launcher_sha256": "c" * 64,
        "listen_host": "127.0.0.1",
        "listen_port": 18000,
        "model_id": "example/model",
        "offloaded_layers": None,
        "receipt_sha256": "a" * 64,
        "resolved_device": None,
        "total_layers": None,
        "upstream": "http://127.0.0.1:18100/v1",
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        validate_proxy_config(ModelProxyConfig(**values))
