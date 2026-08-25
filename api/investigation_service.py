"""Investigation service — adapter between the HTTP layer and the existing
``AnalysisPipeline``.

This module:

- Validates incoming API requests (paths, environment, options).
- Reuses ``GitCollector`` and ``FileCollector`` to gather real evidence.
- Reuses ``AnalysisPipeline`` end-to-end. No reasoning is duplicated.
- Stores completed investigations in an in-memory store keyed by a stable
  investigation ID.
- Exposes typed methods used by ``web_app.py`` to render JSON responses.

The store is process-local and intentionally minimal — the simplest reliable
architecture that satisfies the Investigation UI requirements.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from collectors.file_collector import FileCollector, FileCollectorError
from collectors.git_collector import GitCollector, GitCollectorError
from pipeline import AnalysisPipeline, PipelineInput, PipelineResult

from api.security import (
    resolve_repository_path,
    validate_environment,
    validate_optional_path,
)
from api.serializers import investigation_payload


logger = logging.getLogger(__name__)


class AnalysisRequestError(ValueError):
    """Raised for any 4xx-class failure in the API request."""


@dataclass
class Investigation:
    """In-memory record of a completed investigation."""

    investigation_id: str
    status: str  # "completed" | "failed" | "no_root_cause"
    created_at: str
    duration_ms: Optional[int]
    repository: str
    repository_full_name: str
    environment: str
    branch: str
    commit_sha: Optional[str]
    payload: Dict[str, Any]
    error: Optional[str] = None
    inputs: Dict[str, Any] = field(default_factory=dict)


class InvestigationService:
    """Thread-safe registry of investigations for the current process."""

    def __init__(self, pipeline: AnalysisPipeline) -> None:
        self._pipeline = pipeline
        self._lock = threading.Lock()
        self._investigations: Dict[str, Investigation] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def create_investigation(self, request: Dict[str, Any]) -> Investigation:
        """Validate the request, run the pipeline, and persist the result."""
        try:
            repo = validate_request(request)
            resolved_repo = resolve_repository_path(repo)
            environment = validate_environment(request["environment"])

            traceback_path = validate_optional_path("traceback", request.get("traceback"))
            docker_log_path = validate_optional_path("docker_log", request.get("docker_log"))
            ci_log_path = validate_optional_path("ci_log", request.get("ci_log"))
        except ValueError as exc:
            raise AnalysisRequestError(str(exc)) from exc

        commit_arg = (request.get("commit") or "").strip() or None
        full_name = (request.get("full_name") or "").strip() or resolved_repo.name
        skip_diff = bool(request.get("no_diff", False))

        investigation_id = f"INV-{uuid.uuid4().hex[:12].upper()}"

        try:
            git = GitCollector(str(resolved_repo))
        except GitCollectorError as exc:
            raise AnalysisRequestError(str(exc)) from exc

        sources: Dict[str, str] = {}
        if not skip_diff:
            try:
                sources["git_diff"] = git.collect_diff(commit=commit_arg)
            except GitCollectorError as exc:
                logger.info("git diff skipped: %s", exc)

        for field_name, source_key, path in (
            ("traceback", "traceback", traceback_path),
            ("docker_log", "docker_output", docker_log_path),
            ("ci_log", "ci_log", ci_log_path),
        ):
            if path:
                try:
                    sources[source_key] = FileCollector.collect(path)
                except FileCollectorError as exc:
                    raise AnalysisRequestError(str(exc)) from exc

        if not sources:
            raise AnalysisRequestError(
                "no incident data collected: provide a real traceback/log path "
                "or allow Git diff collection"
            )

        try:
            commit_sha = commit_arg or git.resolve_commit_sha()
        except GitCollectorError:
            commit_sha = "unknown"
        try:
            branch = git.resolve_branch()
        except GitCollectorError:
            branch = "unknown"

        analysis_id = f"AR{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        start = time.time()
        try:
            result: PipelineResult = self._pipeline.run(
                PipelineInput(
                    analysis_id=analysis_id,
                    sources=sources,
                    environment=environment,
                    commit_sha=commit_sha if commit_sha != "unknown" else None,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise AnalysisRequestError(f"pipeline failure: {exc}") from exc
        duration_ms = int((time.time() - start) * 1000)

        selected = result.selected
        if selected is None:
            confidence = None
            severity = None
        else:
            confidence = self._pipeline.compute_confidence(selected.score)
            try:
                severity = self._pipeline.resolve_severity(
                    selected.failure_type_id,
                    {"environment": environment},
                )
            except Exception:
                severity = None

        status = "completed" if selected is not None else "no_root_cause"
        payload = investigation_payload(
            investigation_id=investigation_id,
            repository=str(resolved_repo),
            repository_full_name=full_name,
            environment=environment,
            branch=branch,
            commit_sha=commit_sha,
            status=status,
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            duration_ms=duration_ms,
            selected_hypothesis=selected,
            confidence=confidence,
            severity=severity,
            observations=result.observations,
            evidence=result.evidence_list,
            hypotheses=result.hypotheses,
            timeline=result.timeline,
            correlation=result.correlation,
            graph=result.graph,
            fingerprint=result.fingerprint,
            remediation=result.remediation,
            hypothesis_assessment=result.hypothesis_assessment,
        )

        investigation = Investigation(
            investigation_id=investigation_id,
            status=status,
            created_at=payload["created_at"],
            duration_ms=duration_ms,
            repository=str(resolved_repo),
            repository_full_name=full_name,
            environment=environment,
            branch=branch,
            commit_sha=commit_sha,
            payload=payload,
            inputs={
                "traceback": traceback_path,
                "docker_log": docker_log_path,
                "ci_log": ci_log_path,
                "commit": commit_arg,
                "full_name": full_name,
                "no_diff": skip_diff,
            },
        )

        with self._lock:
            self._investigations[investigation_id] = investigation
        return investigation

    def list_investigations(self) -> List[Investigation]:
        with self._lock:
            items = list(self._investigations.values())
        # Most recent first
        items.sort(key=lambda inv: inv.created_at, reverse=True)
        return items

    def get_investigation(self, investigation_id: str) -> Optional[Investigation]:
        with self._lock:
            return self._investigations.get(investigation_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @property
    def pipeline(self) -> AnalysisPipeline:
        return self._pipeline


# ---------------------------------------------------------------------------
# Helpers used by web_app.py to format list/summary responses.
# ---------------------------------------------------------------------------
def investigation_to_response(inv: Investigation) -> Dict[str, Any]:
    """Render an investigation as a compact list/summary record."""
    payload = inv.payload
    summary = payload.get("incident_summary", {})
    return {
        "investigation_id": inv.investigation_id,
        "status": inv.status,
        "created_at": inv.created_at,
        "duration_ms": inv.duration_ms,
        "repository": inv.repository,
        "repository_full_name": inv.repository_full_name,
        "environment": inv.environment,
        "branch": inv.branch,
        "commit_sha": inv.commit_sha,
        "root_cause": summary.get("root_cause"),
        "root_cause_id": summary.get("root_cause_id"),
        "failure_type_id": summary.get("failure_type_id"),
        "confidence": summary.get("confidence"),
        "severity": summary.get("severity"),
        "evidence_count": len(payload.get("evidence") or []),
        "observation_count": len(payload.get("observations") or []),
        "hypothesis_count": len(payload.get("hypotheses") or []),
        "affected_service": (payload.get("fingerprint") or {}).get("affected_service"),
        "failure_stage": (payload.get("fingerprint") or {}).get("failure_stage"),
    }


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------
def validate_request(request: Any) -> str:
    """Validate the API request body. Returns the resolved repository path."""
    if not isinstance(request, dict):
        raise AnalysisRequestError("request body must be a JSON object")
    repo = request.get("repo")
    if not repo or not isinstance(repo, str):
        raise AnalysisRequestError("a real Git repository path is required (repo)")
    env = request.get("environment")
    if env not in {"local", "staging", "production"}:
        raise AnalysisRequestError(
            "environment must be one of: local, staging, production"
        )
    return repo
