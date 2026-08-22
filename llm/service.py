from __future__ import annotations

from typing import Any, Dict

from rca_request.rca_request_builder import RCARequest
from validation.final_rca_validator import FinalRCAValidator
from llm.client import LLMClient


class LLMAnalysisService:
    """Runs provider generation and applies the deterministic validator gate."""

    def __init__(self, client: LLMClient, validator: FinalRCAValidator | None = None) -> None:
        self._client = client
        self._validator = validator or FinalRCAValidator()

    def generate_validated(self, request: RCARequest) -> Dict[str, Any]:
        final_rca = self._client.generate(request)
        self._validator.validate_or_raise(request.to_dict(), final_rca)
        return final_rca