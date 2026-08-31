"""
collectors/github_actions_collector.py
-----------------------------------------------------------------------------
GitHubActionsCollector — Phase 2.3.

Read-only GitHub Actions integration. Hits the GitHub REST API over
HTTPS using ``urllib.request`` (stdlib only — no new dependency) to
fetch workflow runs for a single repository within a bounded time
window.

Endpoints used (token-authenticated):

- ``GET /repos/{owner}/{repo}/actions/runs?created=>=…&created=<…``
- ``GET /repos/{owner}/{repo}/actions/runs/{id}/jobs`` (per run)

Auth via env var (``auth_env``). Bearer (default) or basic.

Hard limits:

- Window clamped by ``clamp_window`` (max 7 days, min 60 s).
- ``MAX_PIPELINES_PER_WINDOW = 100`` (defined in
  ``cicd_provider_base``).
- Body cap: 5 MiB.
- Auth: bearer or basic. Secrets never logged; scrubbed from errors.

The collector never touches ``engine/``, never invents observations,
and never invents root cause. Returns ``CollectedItem``s that the
``GitHubActionsExtractor`` parses into ``Observation``s.
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


class GitHubActionsCollector(BaseCollector, CICDProvider):
    """Read-only GitHub Actions collector."""

    name = "github_actions"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "github_actions":
            raise IntegrationError(
                f"GitHubActionsCollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'github_actions'"
            )
        if not config.resource:
            raise IntegrationError(
                "GitHubActionsCollector requires IntegrationConfig.resource "
                "(e.g. 'octocat/Hello-World')"
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
        """Provider-neutral entry point. ``repo`` is accepted for
        protocol conformance; this implementation always uses
        ``IntegrationConfig.resource``."""
        return self._fetch_workflow_runs(since, until)

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

        runs = self._fetch_workflow_runs(start, end)
        for run in runs:
            self._populate_jobs(run)

        envelope = {
            "type": "github_actions_runs",
            "repo": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "run_count": len(runs),
            "runs": [r.to_dict() for r in runs],
        }
        envelope_text = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True
        )

        item = CollectedItem(
            source="github_actions",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "repo": cfg.resource,
                "run_count": len(runs),
            },
        )

        meta = {
            "source": "github_actions",
            "endpoint": cfg.endpoint,
            "repo": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "run_count": len(runs),
        }
        return [item], meta

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _fetch_workflow_runs(
        self,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[PipelineRun]:
        cfg = self._config
        params: Dict[str, str] = {
            "per_page": str(min(100, cfg.size)),
        }
        merged = self._merge_params(params, since, until)
        url = (
            _join_url(cfg.endpoint, f"/repos/{cfg.resource}/actions/runs")
            + "?"
            + _urlencode(merged)
        )

        raw_text, status = self._http_get(url)
        if status >= 400:
            raise IntegrationError(
                f"GitHubActionsCollector returned HTTP {status}: "
                f"{_scrub_text(raw_text)[:256]}"
            )
        # Parse response and filter runs based on the incident time window.
        try:
            payload = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"GitHubActionsCollector received malformed JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            runs_raw = []
        else:
            runs_raw = payload.get("workflow_runs") or []
        filtered = []
        for entry in runs_raw[:MAX_PIPELINES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            created = entry.get("created_at")
            if isinstance(created, str):
                try:
                    created_dt = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                except Exception:
                    created_dt = None
                if since is not None and (created_dt is None or created_dt < since):
                    continue
                if until is not None and (created_dt is None or created_dt > until):
                    continue
            filtered.append(entry)
        runs: List[PipelineRun] = []
        for entry in filtered:
            head_branch = entry.get("head_branch")
            head_sha = entry.get("head_sha")
            environment = None
            runs.append(
                PipelineRun(
                    id=str(entry.get("id") or ""),
                    kind="github_actions",
                    status=str(entry.get("status") or "") or None,
                    name=(
                        str(entry.get("name") or "")
                        if isinstance(entry.get("name"), str)
                        else None
                    ),
                    branch=head_branch if isinstance(head_branch, str) else None,
                    commit_sha=head_sha if isinstance(head_sha, str) else None,
                    environment=environment,
                    started_at=(
                        str(entry.get("run_started_at") or "")
                        if isinstance(entry.get("run_started_at"), str)
                        else None
                    ),
                    finished_at=(
                        str(entry.get("updated_at") or "")
                        if isinstance(entry.get("updated_at"), str)
                        else None
                    ),
                    url=(
                        str(entry.get("html_url") or "")
                        if isinstance(entry.get("html_url"), str)
                        else None
                    ),
                    actor=(
                        ((entry.get("actor") or {}).get("login"))
                        if isinstance(entry.get("actor"), dict)
                        else None
                    ),
                    conclusion=(
                        str(entry.get("conclusion") or "")
                        if isinstance(entry.get("conclusion"), str)
                        else None
                    ),
                    failure_message=None,
                    stages=[],
                )
            )
        return runs

    def _merge_params(
        self,
        base: Dict[str, str],
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> Dict[str, str]:
        """Build the ``created`` qualifier correctly. GitHub accepts
        ``created=YYYY-MM-DD..YYYY-MM-DD`` (closed interval) or single
        bounds prefixed with ``>=`` / ``<``."""
        merged: Dict[str, str] = {}
        for k, v in base.items():
            if k != "created":
                merged[k] = v
        if since is not None and until is not None:
            merged["created"] = (
                f"{_to_github_iso(since)}..{_to_github_iso(until)}"
            )
        elif since is not None:
            merged["created"] = f">={_to_github_iso(since)}"
        elif until is not None:
            merged["created"] = f"<{_to_github_iso(until)}"
        return merged

    def _parse_runs(self, raw_text: str) -> List[PipelineRun]:
        try:
            payload = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"GitHubActionsCollector received malformed JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            return []
        runs_raw = payload.get("workflow_runs") or []
        if not isinstance(runs_raw, list):
            return []
        runs: List[PipelineRun] = []
        for entry in runs_raw[:MAX_PIPELINES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            head_branch = entry.get("head_branch")
            head_sha = entry.get("head_sha")
            environment = None
            # GitHub exposes environment in a separate endpoint per
            # run; we leave it None here and let downstream extractors
            # fill it in if the run has a deployment linkage.
            runs.append(
                PipelineRun(
                    id=str(entry.get("id") or ""),
                    kind="github_actions",
                    status=str(entry.get("status") or "") or None,
                    name=(
                        str(entry.get("name") or "")
                        if isinstance(entry.get("name"), str)
                        else None
                    ),
                    branch=head_branch if isinstance(head_branch, str) else None,
                    commit_sha=head_sha if isinstance(head_sha, str) else None,
                    environment=environment,
                    started_at=(
                        str(entry.get("run_started_at") or "")
                        if isinstance(entry.get("run_started_at"), str)
                        else None
                    ),
                    finished_at=(
                        str(entry.get("updated_at") or "")
                        if isinstance(entry.get("updated_at"), str)
                        else None
                    ),
                    url=(
                        str(entry.get("html_url") or "")
                        if isinstance(entry.get("html_url"), str)
                        else None
                    ),
                    actor=(
                        ((entry.get("actor") or {}).get("login"))
                        if isinstance(entry.get("actor"), dict)
                        else None
                    ),
                    conclusion=(
                        str(entry.get("conclusion") or "")
                        if isinstance(entry.get("conclusion"), str)
                        else None
                    ),
                    failure_message=None,
                    stages=[],
                )
            )
        return runs

    def _populate_jobs(self, run: PipelineRun) -> None:
        """Fetch jobs for a single run and project them as stages."""
        url = (
            _join_url(
                self._config.endpoint,
                f"/repos/{self._config.resource}/actions/runs/{run.id}/jobs",
            )
            + "?per_page=" + str(min(100, self._config.size))
        )
        try:
            raw_text, status = self._http_get(url)
        except IntegrationError:
            return
        if status >= 400:
            return
        try:
            payload = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        jobs_raw = payload.get("jobs") or []
        if not isinstance(jobs_raw, list):
            return
        stages: List[PipelineStage] = []
        failure_message: Optional[str] = None
        for entry in jobs_raw[:MAX_STAGES_PER_PIPELINE]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")[:128] or "<job>"
            conclusion = entry.get("conclusion")
            status_str = entry.get("status")
            stages.append(
                PipelineStage(
                    name=name,
                    status=str(status_str) if isinstance(status_str, str) else None,
                    conclusion=str(conclusion) if isinstance(conclusion, str) else None,
                    started_at=(
                        str(entry.get("started_at") or "")
                        if isinstance(entry.get("started_at"), str)
                        else None
                    ),
                    finished_at=(
                        str(entry.get("completed_at") or "")
                        if isinstance(entry.get("completed_at"), str)
                        else None
                    ),
                    failure_message=None,
                )
            )
            if isinstance(conclusion, str) and conclusion == "failure" and not failure_message:
                steps = entry.get("steps") or []
                if isinstance(steps, list):
                    for step in steps:
                        if not isinstance(step, dict):
                            continue
                        if step.get("conclusion") == "failure":
                            failure_message = project_failure_message(
                                step.get("name") or ""
                            ) or "step failed"
                            break
        # We need a mutable copy because PipelineRun is frozen.
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
                    f"GitHubActionsCollector timed out after {timeout}s"
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
                    f"GitHubActionsCollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"GitHubActionsCollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"GitHubActionsCollector network error: {exc}"
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
                f"GitHubActionsCollector response was not decodable: {exc}"
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
            "Accept": "application/vnd.github+json",
            "User-Agent": "AutoRCA-GitHubActionsCollector/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            if self._config.auth_scheme == "basic":
                import base64

                encoded = base64.b64encode(
                    secret.encode("utf-8")
                ).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
            else:
                headers["Authorization"] = f"Bearer {secret}"
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
                f"GitHubActionsCollector response exceeded {max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _scrub_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(
        r"(?i)authorization\s*[:=]\s*\S.*", "authorization: ***", text
    )
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+", "bearer ***", text)
    text = re.sub(
        r"(?i)(?:private-token|token)\s*[:=]\s*\S.*",
        "token: ***",
        text,
    )
    return text[:512]


def _join_url(base: str, suffix: str) -> str:
    base = base.rstrip("/")
    suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    return f"{base}{suffix}"


def _urlencode(params: Dict[str, Any]) -> str:
    return urllib.parse.urlencode(params)


def _to_github_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "GitHubActionsCollector",
    "PipelineRun",
    "PipelineStage",
]
