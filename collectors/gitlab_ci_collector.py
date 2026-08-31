"""
collectors/gitlab_ci_collector.py
-----------------------------------------------------------------------------
GitLabCICollector — Phase 2.3.

Read-only GitLab CI integration. Hits the GitLab REST API over HTTPS
using ``urllib.request`` (stdlib only — no new dependency) to fetch
pipelines + jobs for a single project within a bounded time window.

Endpoints used:

- ``GET /api/v4/projects/{url-encoded project_path}/pipelines``
  filtered by ``updated_after`` / ``updated_before`` and ``ref``
- ``GET /api/v4/projects/{url-encoded project_path}/pipelines/{id}/jobs``
  (per pipeline, bounded to ``MAX_STAGES_PER_PIPELINE``)

Auth via env var (``auth_env``). Default scheme: ``PRIVATE-TOKEN``;
``bearer`` (``auth_scheme="bearer"``) is supported for OAuth2 tokens.

Hard limits:

- Window clamped by ``clamp_window`` (max 7 days, min 60 s).
- ``MAX_PIPELINES_PER_WINDOW = 100``.
- Body cap: 5 MiB.
- Auth: PRIVATE-TOKEN or bearer. Secrets never logged; scrubbed.

The collector never touches ``engine/`` and never invents root cause.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import socket
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from collectors.base import BaseCollector, CollectedItem, IncidentContext
from collectors.cicd_provider_base import (
    CICDProvider,
    MAX_PIPELINES_PER_WINDOW,
    MAX_STAGES_PER_PIPELINE,
    PipelineRun,
    PipelineStage,
    _resolve_window,
    project_failure_message,
)
from collectors.integration_base import (
    IntegrationConfig,
    IntegrationError,
    IntegrationResult,
)


logger = logging.getLogger(__name__)


# Hard cap on raw response body.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class GitLabCICollector(BaseCollector, CICDProvider):
    """Read-only GitLab CI collector."""

    name = "gitlab_ci"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "gitlab_ci":
            raise IntegrationError(
                f"GitLabCICollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'gitlab_ci'"
            )
        if not config.resource:
            raise IntegrationError(
                "GitLabCICollector requires IntegrationConfig.resource "
                "(e.g. 'mygroup/mysubgroup/myproject')"
            )
        self._config = config
        self._cached_secret: Optional[str] = None
        self._secret_resolved = False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        if not self._config.endpoint:
            return False
        if not self._config.resource:
            return False
        return True

    # ------------------------------------------------------------------
    # BaseCollector surface
    # ------------------------------------------------------------------
    def collect(
        self, ctx: Optional[IncidentContext] = None
    ) -> List[CollectedItem]:
        return self._collect_items(ctx)[0]

    def collect_with_metadata(
        self, ctx: Optional[IncidentContext] = None
    ) -> IntegrationResult:
        items, meta = self._collect_items(ctx)
        return IntegrationResult(items=tuple(items), metadata=meta)

    # ------------------------------------------------------------------
    # CICDProvider
    # ------------------------------------------------------------------
    def collect_pipelines(
        self,
        repo: str,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
        ctx: Optional[IncidentContext] = None,
    ) -> List[PipelineRun]:
        return self._fetch_pipelines(since, until)

    # ------------------------------------------------------------------
    # Internal collection
    # ------------------------------------------------------------------
    def _collect_items(
        self, ctx: Optional[IncidentContext] = None
    ) -> Tuple[List[CollectedItem], Dict[str, Any]]:
        cfg = self._config
        start, end = _resolve_window(
            cfg.incident_start, cfg.incident_end, ctx
        )

        runs = self._fetch_pipelines(start, end)
        for run in runs:
            self._populate_jobs(run)

        pipelines = [{**r.to_dict(), "id": int(r.id)} for r in runs]
        envelope = {
            "type": "gitlab_ci_pipelines",
            "project": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "run_count": len(runs),
            "pipelines": pipelines,
        }
        envelope_text = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True
        )

        item = CollectedItem(
            source="gitlab_ci",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "project": cfg.resource,
                "run_count": len(runs),
            },
        )

        meta = {
            "source": "gitlab_ci",
            "endpoint": cfg.endpoint,
            "project": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "run_count": len(runs),
        }
        return [item], meta

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _fetch_pipelines(
        self,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[PipelineRun]:
        cfg = self._config
        params: Dict[str, str] = {
            "per_page": str(min(100, cfg.size)),
            "order_by": "updated_at",
            "sort": "desc",
        }
        if since is not None:
            params["updated_after"] = _to_gitlab_iso(since)
        if until is not None:
            params["updated_before"] = _to_gitlab_iso(until)
        if cfg.query:
            params["ref"] = cfg.query

        project_path = _encode_project_path(cfg.resource)
        url = (
            _join_url(
                cfg.endpoint,
                f"/api/v4/projects/{project_path}/pipelines",
            )
            + "?"
            + _urlencode(params)
        )
        raw_text, status = self._http_get(url)
        if status >= 400:
            raise IntegrationError(
                f"GitLabCICollector pipelines endpoint returned HTTP "
                f"{status}: {_scrub_text(raw_text)[:256]}"
            )
        return self._parse_pipelines(raw_text)

    def _parse_pipelines(self, raw_text: str) -> List[PipelineRun]:
        try:
            payload = json.loads(raw_text) if raw_text else []
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"GitLabCICollector received malformed JSON: {exc}"
            ) from exc
        if not isinstance(payload, list):
            return []
        runs: List[PipelineRun] = []
        for entry in payload[:MAX_PIPELINES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            user_obj = entry.get("user") or {}
            runs.append(
                PipelineRun(
                    id=str(entry.get("id") or ""),
                    kind="gitlab_ci",
                    status=str(entry.get("status") or "") or None,
                    name=None,
                    branch=(
                        str(entry.get("ref") or "")
                        if isinstance(entry.get("ref"), str)
                        else None
                    ),
                    commit_sha=(
                        str(entry.get("sha") or "")
                        if isinstance(entry.get("sha"), str)
                        else None
                    ),
                    environment=(
                        (entry.get("source") or None)
                        if isinstance(entry.get("source"), str)
                        else None
                    ),
                    started_at=(
                        str(entry.get("created_at") or "")
                        if isinstance(entry.get("created_at"), str)
                        else None
                    ),
                    finished_at=(
                        str(entry.get("updated_at") or "")
                        if isinstance(entry.get("updated_at"), str)
                        else None
                    ),
                    url=(
                        str(entry.get("web_url") or "")
                        if isinstance(entry.get("web_url"), str)
                        else None
                    ),
                    actor=(
                        str(user_obj.get("username") or "")
                        if isinstance(user_obj.get("username"), str)
                        else None
                    ),
                    conclusion=(
                        str(entry.get("status") or "")
                        if isinstance(entry.get("status"), str)
                        else None
                    ),
                    failure_message=None,
                    stages=[],
                )
            )
        return runs

    def _populate_jobs(self, run: PipelineRun) -> None:
        cfg = self._config
        project_path = _encode_project_path(cfg.resource)
        url = (
            _join_url(
                cfg.endpoint,
                f"/api/v4/projects/{project_path}/pipelines/{run.id}/jobs",
            )
            + "?per_page=" + str(min(100, cfg.size))
        )
        try:
            raw_text, status = self._http_get(url)
        except IntegrationError:
            return
        if status >= 400:
            return
        try:
            payload = json.loads(raw_text) if raw_text else []
        except json.JSONDecodeError:
            return
        if not isinstance(payload, list):
            return
        stages: List[PipelineStage] = []
        failure_message: Optional[str] = None
        for entry in payload[:MAX_STAGES_PER_PIPELINE]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")[:128] or "<job>"
            stage = entry.get("stage")
            display_name = (
                f"{stage}/{name}" if isinstance(stage, str) and stage else name
            )
            conclusion = entry.get("status")
            stages.append(
                PipelineStage(
                    name=display_name,
                    status=str(conclusion) if isinstance(conclusion, str) else None,
                    conclusion=str(conclusion) if isinstance(conclusion, str) else None,
                    started_at=(
                        str(entry.get("started_at") or "")
                        if isinstance(entry.get("started_at"), str)
                        else None
                    ),
                    finished_at=(
                        str(entry.get("finished_at") or "")
                        if isinstance(entry.get("finished_at"), str)
                        else None
                    ),
                    failure_message=project_failure_message(
                        entry.get("failure_reason")
                    ),
                )
            )
            if (
                isinstance(conclusion, str)
                and conclusion in ("failed", "canceled")
                and not failure_message
            ):
                failure_message = project_failure_message(
                    entry.get("failure_reason")
                ) or "job failed"
        object.__setattr__(run, "stages", stages)
        if failure_message and not run.failure_message:
            object.__setattr__(run, "failure_message", failure_message)

    # ------------------------------------------------------------------
    # HTTP boundary
    # ------------------------------------------------------------------
    def _http_get(self, url: str) -> Tuple[str, int]:
        import urllib.error
        import urllib.request

        secret = self._resolve_secret()
        headers = self._build_headers(secret)

        request = urllib.request.Request(url, headers=headers, method="GET")
        previous_default_timeout = socket.getdefaulttimeout()
        timeout = float(self._config.timeout_seconds)
        socket.setdefaulttimeout(timeout)
        try:
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
            except socket.timeout as exc:
                raise IntegrationError(
                    f"GitLabCICollector timed out after {timeout}s"
                ) from exc
            except urllib.error.HTTPError as exc:
                snippet = ""
                try:
                    body_bytes = exc.read()
                    if isinstance(body_bytes, (bytes, bytearray)):
                        snippet = body_bytes[:4096].decode(
                            "utf-8", errors="replace"
                        )
                except Exception:
                    snippet = ""
                raise IntegrationError(
                    f"GitLabCICollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"GitLabCICollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"GitLabCICollector network error: {exc}"
                ) from exc
            status = int(getattr(response, "status", 200) or 200)
            try:
                raw_bytes = _safe_read(response, max_bytes=MAX_RESPONSE_BYTES)
            finally:
                try:
                    response.close()
                except Exception:  # pragma: no cover - defensive
                    pass
        finally:
            socket.setdefaulttimeout(previous_default_timeout)

        try:
            raw_text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover - defensive
            raise IntegrationError(
                f"GitLabCICollector response was not decodable: {exc}"
            ) from exc
        return raw_text, status

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------
    def _resolve_secret(self) -> Optional[str]:
        if self._secret_resolved:
            return self._cached_secret
        self._secret_resolved = True
        name = self._config.auth_env
        if not name:
            return None
        value = os.environ.get(name)
        if not value:
            return None
        self._cached_secret = value
        return value

    def _build_headers(self, secret: Optional[str]) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "AutoRCA-GitLabCICollector/1.0",
        }
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            if self._config.auth_scheme == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            else:
                # GitLab default: PRIVATE-TOKEN header.
                headers["PRIVATE-TOKEN"] = secret
        return headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_read(response, *, max_bytes: int) -> bytes:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise IntegrationError(
                f"GitLabCICollector response exceeded {max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _scrub_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(
        r"(?i)authorization\s*[:=]\s*\S.*", "authorization: ***", text
    )
    text = re.sub(
        r"(?i)private-token\s*[:=]\s*\S.*", "private-token: ***", text
    )
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+", "bearer ***", text)
    return text[:512]


def _join_url(base: str, suffix: str) -> str:
    base = base.rstrip("/")
    suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    return f"{base}{suffix}"


def _urlencode(params: Dict[str, Any]) -> str:
    return urllib.parse.urlencode(params)


def _encode_project_path(path: str) -> str:
    return urllib.parse.quote(path, safe="")


def _to_gitlab_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "GitLabCICollector",
    "PipelineRun",
    "PipelineStage",
]
