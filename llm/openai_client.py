from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import jsonschema

from rca_request.rca_request_builder import RCARequest
from llm.client import LLMClientError
from llm.prompts import build_messages

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "final_rca.schema.json"


class OpenAICompatibleLLMClient:
    """OpenAI Chat Completions-compatible provider using only the standard library."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._api_key = api_key or os.getenv("AUTORCA_LLM_API_KEY")
        self._model = model or os.getenv("AUTORCA_LLM_MODEL")
        self._base_url = (base_url or os.getenv(
            "AUTORCA_LLM_BASE_URL", "https://api.openai.com/v1"
        )).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._final_schema = self._load_schema()

        if not self._api_key:
            raise LLMClientError(
                "AUTORCA_LLM_API_KEY is not configured; cannot run real LLM analysis."
            )
        if not self._model:
            raise LLMClientError(
                "AUTORCA_LLM_MODEL is not configured; cannot run real LLM analysis."
            )

    @staticmethod
    def _load_schema() -> Dict[str, Any]:
        try:
            with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
                return json.load(schema_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMClientError(f"Could not load FinalRCA schema: {exc}") from exc

    def generate(self, request: RCARequest) -> Dict[str, Any]:
        request_payload = request.to_dict()
        self._validate_request(request_payload)
        body = {
            "model": self._model,
            "messages": build_messages(request),
            "response_format": {"type": "json_object"},
        }
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=encoded_body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(http_request, timeout=self._timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise LLMClientError(f"LLM request failed: {exc}") from exc

        try:
            provider_response = json.loads(response_body)
            content = provider_response["choices"][0]["message"]["content"]
            final_rca = json.loads(content) if isinstance(content, str) else content
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMClientError(
                f"LLM returned malformed structured output: {exc}"
            ) from exc

        self._validate_final_rca(final_rca)
        return final_rca

    def _validate_request(self, payload: Mapping[str, Any]) -> None:
        try:
            with (SCHEMA_PATH.parent / "rca_request.schema.json").open(encoding="utf-8") as schema_file:
                request_schema = json.load(schema_file)
            jsonschema.validate(payload, request_schema)
        except (OSError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            raise LLMClientError(f"RCARequest failed schema validation: {exc}") from exc

    def _validate_final_rca(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise LLMClientError("LLM structured output must be a JSON object.")
        errors = sorted(
            jsonschema.Draft7Validator(self._final_schema).iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            raise LLMClientError(
                "LLM output failed FinalRCA schema validation: "
                + "; ".join(error.message for error in errors)
            )