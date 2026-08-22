from __future__ import annotations

from typing import Any, Dict, Protocol

from rca_request.rca_request_builder import RCARequest


class LLMClientError(RuntimeError):
    """Raised when an LLM cannot produce a valid structured response."""


class LLMClient(Protocol):
    """Small provider boundary; the deterministic core knows nothing about providers."""

    def generate(self, request: RCARequest) -> Dict[str, Any]:
        """Generate a structured FinalRCA from an RCARequest only."""