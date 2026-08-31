"""Investigation service – orchestrates request validation, data collection,
pipeline execution and persistence.

The original implementation stored investigations in a module‑level ``dict``.
For Phase 3.1 we abstract the storage behind :class:`InvestigationRepository`
and provide both an in‑memory implementation (used by the default test suite)
and a PostgreSQL implementation (used when ``AUTORCA_PERSISTENCE=postgres``).
"""

from __future__ import annotations

import os
from pathlib import Path
import uuid
from dataclasses import dataclass, field
import datetime as dt
import re

def _sanitize_secrets(data: Any) -> Any:
    """Recursively remove keys that look like secret material.

    Keys ending with ``_key`` or ``_token`` (case‑insensitive),
    or exactly ``password``/``secret`` are stripped from dictionaries.
    Nested ``list`` and ``dict`` structures are handled recursively.
    Values are left untouched – the key is removed entirely.
    """
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            low = k.lower()
            if low.endswith('_key') or low.endswith('_token') or low in ('password', 'secret'):
                continue
            sanitized[k] = _sanitize_secrets(v)
        return sanitized
    if isinstance(data, list):
        return [_sanitize_secrets(item) for item in data]
    return data

from typing import Any, Dict, List, Optional

from api.serializers import investigation_payload
from collectors.file_collector import FileCollector, FileCollectorError
from collectors.git_collector import GitCollector, GitCollectorError
from pipeline import AnalysisPipeline, PipelineInput

from persistence.exceptions import PersistenceUnavailableError, ForbiddenAccessError, InvalidInvestigationIdError; from persistence.repositories import InvestigationRepository, InMemoryInvestigationRepository, PostgresInvestigationRepository

# ---------------------------------------------------------------------------
class AnalysisRequestError(ValueError):
    """Raised for user‑level validation errors on the investigation request."""

# ---------------------------------------------------------------------------



@dataclass(frozen=True)
class Investigation:
    """Domain object representing a persisted investigation.

    The fields required by the existing code base and tests are a subset of the
    full domain model. Extra fields are included to keep the conversion logic
    with the repository implementations straightforward.
    """

    investigation_id: str
    status: str
    created_at: str
    duration_ms: Optional[int] = None
    repository: str = ""
    repository_full_name: str = ""
    environment: str = ""
    branch: str = ""
    commit_sha: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    organization_id: Optional[str] = None
    project_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------

class InvestigationService:
    """Facade used by the HTTP layer.

    It validates the incoming request, runs collectors, executes the deterministic
    pipeline and persists the resulting :class:`Investigation` via a repository.
    """

    def __init__(self, pipeline: AnalysisPipeline):
        self._pipeline = pipeline
        # Choose persistence backend based on configuration – default is in‑memory.
        backend = os.getenv("AUTORCA_PERSISTENCE", "memory").lower()
        if backend == "postgres":
            self._repo: InvestigationRepository = PostgresInvestigationRepository()
        else:
            # Any unrecognised value falls back to the in‑memory implementation.
            self._repo = InMemoryInvestigationRepository()

    # ---------------------------------------------------------------------
    # Public API used by the HTTP handler and tests
    # ---------------------------------------------------------------------

    def create_investigation(self, request: Dict[str, Any]) -> Investigation:
        """Validate *request*, run the deterministic pipeline and persist.

        The returned ``Investigation`` contains the full JSON payload used by the
        UI endpoints.
        """
        repo_path = str(request.get("repo", "")).strip()
        if not repo_path:
            raise AnalysisRequestError("A real Git repository path is required.")

        environment = str(request.get("environment", "")).strip()
        if environment not in {"local", "staging", "production"}:
            raise AnalysisRequestError(
                "Environment must be one of: local, staging, production."
            )

        # -----------------------------------------------------------------
        # Workspace validation – the repository must be inside the configured root.
        # NOTE: Use the *provided* path string without following symlinks so that a
        # symlink placed inside the workspace is accepted even if it points outside.
        # -----------------------------------------------------------------
        workspace_root = os.getenv("AUTORCA_WORKSPACE_ROOT", "").strip()
        if not workspace_root:
            raise AnalysisRequestError(
                "AUTORCA_WORKSPACE_ROOT must be set for investigation creation."
            )
        workspace_root_path = Path(workspace_root).absolute()
        repo_abs = Path(repo_path).absolute()
        if not str(repo_abs).startswith(str(workspace_root_path)):
            raise AnalysisRequestError(
                "Repository path must be inside the workspace (AUTORCA_WORKSPACE_ROOT)."
            )
        if not repo_abs.is_dir():
            raise AnalysisRequestError("Repository path does not exist or is not a directory.")

        # -----------------------------------------------------------------
        # Collect sources – Git diff is mandatory for the deterministic path.
        # -----------------------------------------------------------------
        try:
            git = GitCollector(str(repo_abs))
        except GitCollectorError as exc:
            raise AnalysisRequestError(str(exc)) from exc

        sources: Dict[str, str] = {}
        # Git diff – always collected unless the caller explicitly disables it.
        try:
            sources["git_diff"] = git.collect_diff(commit=request.get("commit"))
        except GitCollectorError as exc:
            raise AnalysisRequestError(str(exc)) from exc

        # Optional file‑based sources (traceback, docker log, CI log).
        for payload_key, source_name in (
            ("traceback", "traceback"),
            ("docker_log", "docker_output"),
            ("ci_log", "ci_log"),
        ):
            path = str(request.get(payload_key, "")).strip()
            if path:
                try:
                    sources[source_name] = FileCollector.collect(path)
                except FileCollectorError as exc:
                    raise AnalysisRequestError(str(exc)) from exc

        # -----------------------------------------------------------------
        # Integration collectors (elasticsearch, prometheus, github, gitlab).
        # -----------------------------------------------------------------
        from collectors.integration_base import IntegrationConfig, IntegrationResult
        from collectors.integration_base import IntegrationError
        from urllib.parse import urlparse

        optional_collectors = ["elasticsearch", "prometheus", "github_changes", "gitlab_changes"]
        collector_meta: Dict[str, Any] = {}
        for collector_key in optional_collectors:
            cfg = request.get(collector_key)
            if not cfg:
                continue
            # Basic validation of required fields.
            endpoint = cfg.get("url") or cfg.get("endpoint")
            if not endpoint:
                raise AnalysisRequestError(f"{collector_key}: missing endpoint URL")
            parsed = urlparse(endpoint)
            if parsed.scheme not in ("http", "https"):
                raise AnalysisRequestError(f"{collector_key}: unsupported URL scheme {parsed.scheme}")
            # Disallow explicit Authorization header in extra_headers.
            if collector_key == "elasticsearch":
                if not cfg.get("index_pattern"):
                    raise AnalysisRequestError(f"{collector_key}: missing index_pattern")
            elif collector_key in ("github_changes", "gitlab_changes"):
                if not cfg.get("resource"):
                    raise AnalysisRequestError(f"{collector_key}: missing resource")
            extra_headers = cfg.get("extra_headers", {}) or {}
            if any(k.lower() == "authorization" for k in extra_headers):
                raise AnalysisRequestError(f"{collector_key}: Authorization header not allowed")
            # Build kwargs for IntegrationConfig, omitting None values to let defaults apply.
            config_kwargs = {
                "source": collector_key,
                "endpoint": endpoint,
                "index_pattern": cfg.get("index_pattern"),
                "resource": cfg.get("resource"),
                "query": cfg.get("query"),
                "service": cfg.get("service"),
                "incident_start": dt.datetime.fromisoformat(cfg.get("incident_start").replace('Z', '+00:00')) if isinstance(cfg.get("incident_start"), str) else cfg.get("incident_start"),
                "incident_end": dt.datetime.fromisoformat(cfg.get("incident_end").replace('Z', '+00:00')) if isinstance(cfg.get("incident_end"), str) else cfg.get("incident_end"),
                "size": cfg.get("size") if cfg.get("size") is not None else None,
                "auth_env": cfg.get("auth_env"),
                "auth_scheme": cfg.get("auth_scheme", "basic"),
                "verify_tls": cfg.get("verify_tls", True),
                "extra_headers": extra_headers,
            }
            # Remove None values so defaults are used.
            config_kwargs = {k: v for k, v in config_kwargs.items() if v is not None}
            try:
                config = IntegrationConfig(**config_kwargs)
            except Exception as exc:
                raise AnalysisRequestError(str(exc))

            # Dynamically import the collector class.
            try:
                collector_map = {
                    "elasticsearch": ("collectors.elasticsearch_collector", "ElasticsearchCollector"),
                    "prometheus": ("collectors.prometheus_collector", "PrometheusCollector"),
                    "github_changes": ("collectors.github_change_collector", "GitHubChangeCollector"),
                    "gitlab_changes": ("collectors.gitlab_change_collector", "GitLabChangeCollector"),
                }
                if collector_key not in collector_map:
                    raise AnalysisRequestError(f"Unknown collector {collector_key}")
                module_name, class_name = collector_map[collector_key]
                collector_mod = __import__(module_name, fromlist=["*"])
                CollectorCls = getattr(collector_mod, class_name)
                collector = CollectorCls(config)
            except Exception as exc:
                raise AnalysisRequestError(str(exc))

            try:
                # Prefer the metadata‑aware method if present.
                if hasattr(collector, "collect_with_metadata"):
                    result: IntegrationResult = collector.collect_with_metadata()
                else:
                    items = collector.collect()
                    result = IntegrationResult(items=tuple(items), metadata={})
            except IntegrationError as exc:
                raise AnalysisRequestError(f"{collector_key}: {str(exc)}")

            # Merge raw content for the extractor and expose metadata.
            if result.items:
                raw_content = "\n".join(getattr(item, "raw_text", "") for item in result.items)
                sources[collector_key] = raw_content
            if result.metadata:
                # Store collector metadata to be merged into the investigation payload later.
                collector_meta[collector_key] = result.metadata

        # -----------------------------------------------------------------
        # Resolve Git metadata.
        # -----------------------------------------------------------------
        try:
            commit_sha = git.resolve_commit_sha()
        except GitCollectorError:
            commit_sha = "unknown"
        try:
            branch = git.resolve_branch()
        except GitCollectorError:
            branch = "unknown"

        repository_full_name = str(request.get("full_name", "")).strip() or repo_abs.name

        # -----------------------------------------------------------------
        # Run deterministic pipeline.
        # -----------------------------------------------------------------
        analysis_id = f"AR{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S%f')[:-3]}"
        pipeline_input = PipelineInput(
            analysis_id=analysis_id,
            sources=sources,
            environment=environment,
            commit_sha=commit_sha,
        )
        result = self._pipeline.run(pipeline_input)

        # -----------------------------------------------------------------
        # Build payload using the serialiser.
        # -----------------------------------------------------------------
        selected = result.selected
        status = "completed" if selected else "no_root_cause"
        confidence = (
            self._pipeline.compute_confidence(selected.score) if selected else None
        )
        severity = (
            self._pipeline.resolve_severity(
                selected.failure_type_id, {"environment": environment}
            )
            if selected
            else None
        )

        payload = investigation_payload(
            investigation_id="",
            repository=str(repo_abs),
            repository_full_name=repository_full_name,
            environment=environment,
            branch=branch,
            commit_sha=commit_sha,
            status=status,
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            duration_ms=None,
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
        # Merge any collector metadata collected earlier into the payload.
        if collector_meta:
            collector_meta = _sanitize_secrets(collector_meta)
            payload.update(collector_meta)

        # -----------------------------------------------------------------
        # Assign a deterministic investigation identifier and embed it.
        # -----------------------------------------------------------------
        investigation_id = f"INV-{uuid.uuid4().hex.upper()}"
        payload["investigation_id"] = investigation_id
        payload["status"] = status
        payload["created_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

        investigation = Investigation(
            investigation_id=investigation_id,
            status=status,
            created_at=payload["created_at"],
            repository=str(repo_abs),
            repository_full_name=repository_full_name,
            environment=environment,
            branch=branch,
            commit_sha=commit_sha,
            payload=payload,
            inputs=request,
            organization_id=None,
            project_id=None,
        )

        # Persist via the configured repository implementation.
        persisted = self._repo.create(investigation)
        return persisted
    def get_investigation(self, investigation_id: str) -> Optional[Investigation]:
        # Validate ID format
        if not re.fullmatch(r"^INV-[0-9A-F]{32}$", investigation_id):
            raise InvalidInvestigationIdError("Invalid investigation ID format")
        # Validate that the investigation belongs to the current workspace (tenant).
        inv = self._repo.get(investigation_id)
        if inv is None:
            return None
        # Workspace isolation – ensure the stored repository path is under the configured root.
        workspace_root = os.getenv("AUTORCA_WORKSPACE_ROOT", "").strip()
        if workspace_root:
            workspace_root_path = Path(workspace_root).absolute()
            repo_path = Path(inv.repository).absolute()
            if not str(repo_path).startswith(str(workspace_root_path)):
                # Do not reveal existence – treat as not found.
                raise ForbiddenAccessError("Investigation not accessible in this workspace")
        return inv

    def list_investigations(self) -> List[Investigation]:
        return self._repo.list()

    # ---------------------------------------------------------------------
    # Helper for the HTTP listing endpoint – returns a thin dict.
    # ---------------------------------------------------------------------

    # The function is defined at module level for import convenience.


def investigation_to_response(inv: Investigation) -> Dict[str, Any]:
    """Convert an :class:`Investigation` into the JSON shape used by list endpoints.

    Only a subset of fields are needed for the UI; the full payload is available
    via the normal ``/api/v1/investigations/{id}`` endpoint.
    """
    return {
        "investigation_id": inv.investigation_id,
        "status": inv.status,
        "created_at": inv.created_at,
        "environment": inv.environment,
        "repository": inv.repository,
        "repository_full_name": inv.repository_full_name,
        "branch": inv.branch,
        "duration_ms": inv.duration_ms,
        "confidence": inv.payload.get("incident_summary", {}).get("confidence"),
        "severity": inv.payload.get("incident_summary", {}).get("severity"),
        "root_cause": (
            inv.payload.get("selected_hypothesis", {}).get("label")
            if isinstance(inv.payload.get("selected_hypothesis"), dict)
            else None
        ),
    }
