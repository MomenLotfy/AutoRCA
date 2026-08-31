"""
collectors/cicd_provider_base.py
-----------------------------------------------------------------------------
CICDProvider — provider-neutral abstraction over CI/CD APIs.

Phase 2.3 — concrete providers ship for:

    - GitHub Actions    (``github_actions``)
    - GitLab CI         (``gitlab_ci``)
    - Jenkins           (``jenkins``)

Every provider is read-only: GET-only HTTP, no triggering, no
cancellation, no approval. The provider exposes a small frozen
contract:

    collect_pipelines(repo, since, until, ctx) -> List[PipelineRun]

A ``PipelineRun`` carries:

    {
      "id":          provider-specific id (str)
      "kind":        "github_actions" | "gitlab_ci" | "jenkins"
      "status":      "success" | "failure" | "cancelled" | "running" | "pending" | "skipped"
      "conclusion":  optional explicit conclusion (GitHub Actions)
      "name":        workflow / pipeline / job name (str)
      "branch":      branch ref (str)
      "commit_sha":  commit SHA (str)
      "environment": deployment environment (str)
      "started_at":  ISO-8601 UTC
      "finished_at": ISO-8601 UTC
      "url":         web URL (optional)
      "actor":       who triggered it
      "stages":      list of {name, status, conclusion, started_at, finished_at}
      "failure_message": optional short failure message (whitelisted)
    }

Stage entries project only the fields the existing RCA engine
consumes — never raw upstream payloads.

Hard caps (defence in depth):

- ``MAX_PIPELINES_PER_WINDOW = 100``
- ``MAX_STAGES_PER_PIPELINE  = 50``
- Window clamped by ``clamp_window`` to 7 days max, 60 s min.
- Body size: 5 MiB cap (per-collector).
- No POST/PUT/PATCH/DELETE ever.
- Auth: env-var name only (``auth_env``), never logged.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from collectors.base import IncidentContext
from collectors.change_provider_base import clamp_window


logger = logging.getLogger(__name__)


# Hard caps shared by every CI/CD provider.
MAX_PIPELINES_PER_WINDOW = 100
MAX_STAGES_PER_PIPELINE = 50


@dataclass(frozen=True)
class PipelineStage:
    """A single stage / job inside a pipeline run."""

    name: str
    status: Optional[str] = None
    conclusion: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    failure_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_message": self.failure_message,
        }


@dataclass(frozen=True)
class PipelineRun:
    """A single CI/CD pipeline run.

    `kind` is the provider identifier (e.g. ``"github_actions"``).
    `stages` is the ordered list of jobs/steps (already bounded to
    ``MAX_STAGES_PER_PIPELINE`` by the provider).
    """

    id: str
    kind: str
    status: Optional[str]
    name: Optional[str]
    branch: Optional[str]
    commit_sha: Optional[str]
    environment: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]
    url: Optional[str] = None
    actor: Optional[str] = None
    conclusion: Optional[str] = None
    stages: List[PipelineStage] = field(default_factory=list)
    failure_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "conclusion": self.conclusion,
            "name": self.name,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "environment": self.environment,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "url": self.url,
            "actor": self.actor,
            "stages": [s.to_dict() for s in self.stages],
            "failure_message": self.failure_message,
        }


@runtime_checkable
class CICDProvider(Protocol):
    """Provider-neutral protocol every CI/CD collector implements.

    Concrete classes additionally extend ``BaseCollector`` from
    ``collectors/base.py``. The provider-neutral entry point is
    ``collect_pipelines``. All other ``BaseCollector`` surface
    (``is_available`` / ``collect`` / ``name``) is unchanged.
    """

    name: str

    def collect_pipelines(
        self,
        repo: str,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
        ctx: Optional[IncidentContext] = None,
    ) -> List[PipelineRun]:
        """Return a bounded list of pipeline runs for the given repo
        in the given time window. The provider is responsible for
        bounding the result count itself (``MAX_PIPELINES_PER_WINDOW``,
        ``MAX_STAGES_PER_PIPELINE``)."""

    def is_available(self) -> bool: ...


# ---------------------------------------------------------------------------
# Helpers shared by GitHub Actions / GitLab CI / Jenkins
# ---------------------------------------------------------------------------
def _resolve_window(
    cfg_start: Optional[dt.datetime],
    cfg_end: Optional[dt.datetime],
    ctx: Optional[IncidentContext],
) -> tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    start = cfg_start
    end = cfg_end
    if ctx is not None and ctx.is_set():
        start = start or ctx.incident_start
        end = end or ctx.incident_end
    if start is None and end is None:
        raise ValueError(
            "CICDProvider requires a time window (incident_start/incident_end)"
        )
    return clamp_window(start, end)


def project_failure_message(text: Optional[str]) -> Optional[str]:
    """Defensive projection of a CI/CD failure message.

    Returns a short (≤ 256 chars) redacted form. If the input is None
    or empty, returns None. We never echo raw provider payloads — only
    the first line of any failure excerpt.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    first_line = text.strip().splitlines()[0] if "\n" in text else text.strip()
    return first_line[:256] or None


__all__ = [
    "CICDProvider",
    "PipelineRun",
    "PipelineStage",
    "MAX_PIPELINES_PER_WINDOW",
    "MAX_STAGES_PER_PIPELINE",
    "_resolve_window",
    "project_failure_message",
]
