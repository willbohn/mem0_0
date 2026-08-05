"""Bounded readiness checks for the configured OpenAI-compatible provider."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


class _ProviderCheckError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_url(value: str) -> str:
    """Return a status-safe URL with credentials and query data removed."""
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return "configured-provider"
    try:
        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        netloc = parsed.hostname
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


class ProviderReadiness:
    """Check endpoint availability and configured model presence."""

    def __init__(
        self,
        *,
        base_url: str,
        llm_model: str,
        embedder_model: str,
        timeout: float | None = None,
        require_loaded: bool | None = None,
        check_native: bool | None = None,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.timeout = max(0.2, min(float(timeout or os.getenv("MEM0_PROVIDER_CHECK_TIMEOUT", "2")), 10.0))
        self.require_loaded = (
            _truthy(os.getenv("MEM0_REQUIRE_LOADED_MODELS")) if require_loaded is None else require_loaded
        )
        self.check_native = _truthy(os.getenv("MEM0_LMSTUDIO_COMPAT")) if check_native is None else check_native
        self.opener = opener
        self.expected = {"llm": llm_model, "embedder": embedder_model}

    def check(self) -> dict[str, Any]:
        models = {
            role: {"model": model, "present": False, "loaded": None}
            for role, model in self.expected.items()
        }
        result: dict[str, Any] = {
            "status": "degraded",
            "code": "provider_unavailable",
            "message": "Configured OpenAI-compatible provider is unavailable.",
            "provider": "openai-compatible",
            "endpoint": _safe_url(self.base_url),
            "models": models,
            "available_models": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            payload = self._get_json(f"{self.base_url}/models")
            available_models = self._model_entries(payload)
        except _ProviderCheckError as exc:
            result["code"] = exc.code
            result["message"] = exc.message
            return result

        available_ids = sorted({self._model_id(item) for item in available_models if self._model_id(item)})
        result["available_models"] = available_ids
        for role, expected_model in self.expected.items():
            models[role]["present"] = expected_model in available_ids

        native_models = self._get_native_lmstudio_models()
        if native_models is not None:
            native_by_id = {self._model_id(item): item for item in native_models if self._model_id(item)}
            for role, model in self.expected.items():
                native = native_by_id.get(model)
                if native is not None:
                    models[role]["loaded"] = self._is_loaded(native)

        missing = [f"{role}:{model}" for role, model in self.expected.items() if not models[role]["present"]]
        if missing:
            result["code"] = "model_missing"
            result["message"] = "Configured model(s) are not advertised by the provider: " + ", ".join(missing)
            return result

        not_loaded = [
            f"{role}:{model}"
            for role, model in self.expected.items()
            if models[role]["loaded"] is False
        ]
        if self.require_loaded and not_loaded:
            result["code"] = "model_not_loaded"
            result["message"] = "Configured model(s) are present but not loaded: " + ", ".join(not_loaded)
            return result

        result["status"] = "ready"
        result["code"] = "ready"
        result["message"] = "Configured provider and models are available."
        if not_loaded:
            result["message"] += " Models may be loaded just in time by the provider."
        return result

    def _get_json(self, url: str) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            with self.opener(Request(url, headers=headers), timeout=self.timeout) as response:
                payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except (HTTPError, TimeoutError, URLError, OSError) as exc:
            raise _ProviderCheckError(
                "provider_unavailable", "Configured OpenAI-compatible provider is unavailable."
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise _ProviderCheckError(
                "provider_invalid_response", "Configured provider returned an invalid model list."
            ) from exc
        if not isinstance(payload, dict):
            raise _ProviderCheckError(
                "provider_invalid_response", "Configured provider returned an invalid model list."
            )
        return payload

    def _get_native_lmstudio_models(self) -> list[dict[str, Any]] | None:
        if not self.check_native:
            return None
        parsed = urlsplit(self.base_url)
        path = parsed.path.rstrip("/")
        if not path.endswith("/v1"):
            return None
        native_path = path[: -len("/v1")] + "/api/v1/models"
        native_url = urlunsplit((parsed.scheme, parsed.netloc, native_path, "", ""))
        try:
            payload = self._get_json(native_url)
        except _ProviderCheckError:
            return None
        models = payload.get("models")
        return models if isinstance(models, list) else None

    @staticmethod
    def _model_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
        entries = payload.get("data")
        if entries is None:
            entries = payload.get("models")
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise _ProviderCheckError("provider_invalid_response", "Configured provider returned an invalid model list.")
        return entries

    @staticmethod
    def _model_id(item: dict[str, Any]) -> str:
        value = item.get("id") or item.get("key")
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _is_loaded(item: dict[str, Any]) -> bool | None:
        if "loaded_instances" in item:
            return bool(item["loaded_instances"])
        state = item.get("state")
        if isinstance(state, str):
            return state.lower() == "loaded"
        return None
