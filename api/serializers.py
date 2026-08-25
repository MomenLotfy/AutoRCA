"""Convert pipeline output to stable JSON-serialisable API payloads.

These serializers are intentionally additive: they consume the existing
``PipelineResult`` (and all its rich fields) and produce dicts that the
HTTP layer can ship without further transformation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api.security import mask_secret
from engine.correlation_engine import CorrelationGraph
from engine.hypothesis_engine import HypothesisAssessmentResult
from engine.incident_fingerprint import IncidentFingerprint
from engine.incident_graph import IncidentGraph
from engine.remediation_engine import RemediationContext
from engine.timeline_engine import IncidentTimeline


# Keys that hold environment-variable names inside the ``data`` dict of
# observations/evidence, so we can mask sensitive values.
_ENV_VAR_NAME_KEYS = {"key", "missing_key", "env_var", "variable", "name"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(value: Any) -> Any:
    """Best-effort serialisation for non-JSON-native values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _maybe_mask_observation_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Mask sensitive environment-variable values for safe display."""
    if not isinstance(data, dict):
        return {"value": _safe(data)}
    if not data:
        return {}
    name = None
    for key in _ENV_VAR_NAME_KEYS:
        if key in data:
            name = str(data[key])
            break
    value = data.get("value")
    if value is not None:
        return {**data, "value": mask_secret(name, str(value))}
    return {k: _safe(v) for k, v in data.items()}


def serialise_observation(obs) -> Dict[str, Any]:
    return {
        "id": obs.id,
        "kind": obs.kind,
        "source": obs.source,
        "producer_id": obs.producer_id,
        "producer_version": obs.producer_version,
        "location": _safe(obs.location.to_dict() if hasattr(obs.location, "to_dict") else obs.location),
        "data": _maybe_mask_observation_data(dict(obs.data)),
        "raw_reference": obs.raw_reference,
        "extracted_at": obs.extracted_at,
    }


def serialise_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
    safe = {k: _safe(v) for k, v in item.items()}
    return safe


def serialise_hypothesis(hyp) -> Dict[str, Any]:
    payload = hyp.to_dict()
    payload["public_id"] = payload.get("id")
    return _safe(payload)


def serialise_timeline(timeline: IncidentTimeline) -> Dict[str, Any]:
    if timeline is None:
        return None  # type: ignore[return-value]
    return timeline.to_dict()


def serialise_correlation(graph: CorrelationGraph) -> Dict[str, Any]:
    if graph is None:
        return None  # type: ignore[return-value]
    return graph.to_dict()


def serialise_incident_graph(graph: IncidentGraph) -> Dict[str, Any]:
    if graph is None:
        return None  # type: ignore[return-value]
    return graph.to_dict()


def serialise_fingerprint(fp: Optional[IncidentFingerprint]) -> Optional[Dict[str, Any]]:
    if fp is None:
        return None
    return fp.to_dict()


def serialise_remediation(rem: Optional[RemediationContext]) -> Optional[Dict[str, Any]]:
    if rem is None:
        return None
    return rem.to_dict()


def serialise_assessment(ass: Optional[HypothesisAssessmentResult]) -> Optional[Dict[str, Any]]:
    if ass is None:
        return None
    return ass.to_dict()


def investigation_payload(
    *,
    investigation_id: str,
    repository: str,
    repository_full_name: str,
    environment: str,
    branch: str,
    commit_sha: Optional[str],
    status: str,
    created_at: str,
    duration_ms: Optional[int],
    selected_hypothesis: Any,
    confidence: Optional[float],
    severity: Optional[str],
    observations: List[Any],
    evidence: List[Dict[str, Any]],
    hypotheses: List[Any],
    timeline: IncidentTimeline,
    correlation: CorrelationGraph,
    graph: IncidentGraph,
    fingerprint: Optional[IncidentFingerprint],
    remediation: Optional[RemediationContext],
    hypothesis_assessment: Optional[HypothesisAssessmentResult],
) -> Dict[str, Any]:
    """Build the canonical investigation payload from pipeline output."""
    return {
        "investigation_id": investigation_id,
        "status": status,
        "created_at": created_at,
        "duration_ms": duration_ms,
        "repository": repository,
        "repository_full_name": repository_full_name,
        "environment": environment,
        "branch": branch,
        "commit_sha": commit_sha,
        "incident_summary": {
            "root_cause": selected_hypothesis.label if selected_hypothesis else None,
            "root_cause_id": getattr(selected_hypothesis, "id", None),
            "failure_type_id": getattr(selected_hypothesis, "failure_type_id", None),
            "confidence": confidence,
            "severity": severity,
        },
        "selected_hypothesis": serialise_hypothesis(selected_hypothesis) if selected_hypothesis else None,
        "observations": [serialise_observation(o) for o in observations],
        "evidence": [serialise_evidence(e) for e in evidence],
        "hypotheses": [serialise_hypothesis(h) for h in hypotheses],
        "timeline": serialise_timeline(timeline),
        "correlation": serialise_correlation(correlation),
        "graph": serialise_incident_graph(graph),
        "fingerprint": serialise_fingerprint(fingerprint),
        "remediation": serialise_remediation(remediation),
        "hypothesis_assessment": serialise_assessment(hypothesis_assessment),
        "generated_at": _now_iso(),
    }
