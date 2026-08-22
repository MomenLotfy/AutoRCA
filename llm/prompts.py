from __future__ import annotations

import json
from typing import Any, Dict

from rca_request.rca_request_builder import RCARequest

SYSTEM_PROMPT = """You are the explanation layer of AutoRCA.

The root cause has already been determined by a deterministic rule engine.
You MUST NOT determine a different root cause or invent evidence.
Use only the supplied RCARequest. Preserve exactly:
- selected_hypothesis_id
- confidence
- cited_evidence_ids
- resolved severity in incident_report

Your role is explanation and remediation guidance only.
Deterministic Reasoning is separate from Generative Explanation.
Return only a JSON object matching the FinalRCA schema.
"""


def build_user_payload(request: RCARequest) -> str:
    """Serialize only the bounded, structured RCARequest for the provider."""
    return json.dumps(request.to_dict(), ensure_ascii=False, separators=(",", ":"))


def build_messages(request: RCARequest) -> list[Dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_payload(request)},
    ]