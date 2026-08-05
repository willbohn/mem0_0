import hashlib
import json

import pytest

from pathlib import Path

from evaluation_overlay.model_proxy import (
    ModelProxyConfig,
    build_llama_command,
    build_models_payload,
    build_proxy_attestation,
    validate_proxy_config,
)


def test_models_payload_attests_exact_receipt_fingerprint():
    fingerprint = hashlib.sha256(b"receipt").hexdigest()
    config = ModelProxyConfig(
        api_key="local-key",
        artifact_path=Path("/tmp/model.gguf"),
        artifact_sha256="b" * 64,
        launcher_sha256="c" * 64,
        listen_host="127.0.0.1",
        listen_port=18000,
        model_id="example/model",
        receipt_sha256=fingerprint,
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
        "kind": "mem0_0_model_proxy_v1",
        "launcher_sha256": "c" * 64,
        "model_id": "example/model",
        "receipt_sha256": fingerprint,
    }


def test_llama_command_binds_exact_artifact_alias_and_role(tmp_path):
    key_file = tmp_path / "api-key"
    llm = ModelProxyConfig(
        api_key="local-key",
        artifact_path=tmp_path / "model.gguf",
        artifact_sha256="b" * 64,
        launcher_sha256="c" * 64,
        listen_host="127.0.0.1",
        listen_port=18000,
        model_id="example/model",
        receipt_sha256="a" * 64,
        upstream="http://127.0.0.1:18100/v1",
    )
    embedder = ModelProxyConfig(**{**llm.__dict__, "model_id": "example/embedder"})

    llm_command = build_llama_command(llm, Path("/usr/bin/llama-server"), key_file, "llm")
    embedder_command = build_llama_command(embedder, Path("/usr/bin/llama-server"), key_file, "embedder")

    assert llm_command[llm_command.index("--model") + 1] == str(llm.artifact_path)
    assert llm_command[llm_command.index("--alias") + 1] == llm.model_id
    assert "--embedding" not in llm_command
    assert "--embedding" in embedder_command
    assert embedder_command[embedder_command.index("--pooling") + 1] == "mean"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("listen_host", "0.0.0.0", "loopback"),
        ("upstream", "https://models.example.com/v1", "loopback"),
        ("api_key", "", "API key"),
        ("receipt_sha256", "bad", "receipt fingerprint"),
    ],
)
def test_proxy_config_fails_closed(field, value, message):
    values = {
        "api_key": "local-key",
        "artifact_path": Path("/tmp/model.gguf"),
        "artifact_sha256": "b" * 64,
        "launcher_sha256": "c" * 64,
        "listen_host": "127.0.0.1",
        "listen_port": 18000,
        "model_id": "example/model",
        "receipt_sha256": "a" * 64,
        "upstream": "http://127.0.0.1:18100/v1",
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        validate_proxy_config(ModelProxyConfig(**values))
