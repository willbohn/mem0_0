import json
from urllib.error import URLError

from server.provider_readiness import ProviderReadiness


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps(self.payload).encode()


def test_provider_readiness_reports_configured_models_and_redacts_endpoint_credentials():
    def opener(request, timeout):
        assert timeout == 2
        assert request.full_url == "http://user:password@127.0.0.1:1234/v1/models"
        return Response({"data": [{"id": "llm-good"}, {"id": "embed-good"}]})

    result = ProviderReadiness(
        base_url="http://user:password@127.0.0.1:1234/v1?token=private",
        llm_model="llm-good",
        embedder_model="embed-good",
        opener=opener,
    ).check()

    assert result["status"] == "ready"
    assert result["code"] == "ready"
    assert result["endpoint"] == "http://127.0.0.1:1234/v1"
    assert "password" not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_provider_readiness_reports_missing_model():
    result = ProviderReadiness(
        base_url="http://127.0.0.1:1234/v1",
        llm_model="llm-missing",
        embedder_model="embed-good",
        opener=lambda *_args, **_kwargs: Response({"data": [{"id": "embed-good"}]}),
    ).check()

    assert result["status"] == "degraded"
    assert result["code"] == "model_missing"
    assert result["models"]["llm"]["present"] is False


def test_provider_readiness_skips_native_lmstudio_probe_without_compatibility_flag(monkeypatch):
    monkeypatch.delenv("MEM0_LMSTUDIO_COMPAT", raising=False)
    requested = []

    def opener(request, timeout):
        requested.append((request.full_url, timeout))
        return Response({"data": [{"id": "llm-good"}, {"id": "embed-good"}]})

    result = ProviderReadiness(
        base_url="http://127.0.0.1:1234/v1",
        llm_model="llm-good",
        embedder_model="embed-good",
        opener=opener,
    ).check()

    assert result["status"] == "ready"
    assert requested == [("http://127.0.0.1:1234/v1/models", 2.0)]


def test_provider_readiness_reports_unavailable_provider_without_credentials():
    def unavailable(*_args, **_kwargs):
        raise URLError("offline")

    result = ProviderReadiness(
        base_url="http://user:password@127.0.0.1:1234/v1",
        llm_model="llm-good",
        embedder_model="embed-good",
        opener=unavailable,
    ).check()

    assert result["code"] == "provider_unavailable"
    assert "password" not in json.dumps(result)


def test_provider_readiness_can_require_native_lmstudio_models_to_be_loaded():
    def opener(request, timeout):
        assert timeout == 2
        if "/api/v1/models" in request.full_url:
            return Response(
                {
                    "models": [
                        {"key": "llm-good", "loaded_instances": []},
                        {"key": "embed-good", "loaded_instances": [{"id": "embed-good"}]},
                    ]
                }
            )
        return Response({"data": [{"id": "llm-good"}, {"id": "embed-good"}]})

    result = ProviderReadiness(
        base_url="http://127.0.0.1:1234/v1",
        llm_model="llm-good",
        embedder_model="embed-good",
        require_loaded=True,
        check_native=True,
        opener=opener,
    ).check()

    assert result["code"] == "model_not_loaded"
    assert result["models"]["llm"]["loaded"] is False
    assert result["models"]["embedder"]["loaded"] is True
