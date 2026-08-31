"""
collectors/jenkins_collector.py
-----------------------------------------------------------------------------
JenkinsCollector — Phase 2.3.

Read-only Jenkins integration. Hits the Jenkins REST API over HTTPS
using ``urllib.request`` (stdlib only — no new dependency) to fetch
builds for a single job within a bounded time window.

Endpoints used:

- ``GET /job/{url-encoded job_path}/api/json?tree=builds[number,timestamp,result,…]``
- ``GET /job/{url-encoded job_path}/{build_number}/api/json``
  (per-build detail; stages are flat here, not matrix-style)

Auth via env var (``auth_env``). Default scheme: ``basic`` (Jenkins
Personal Access Token + username as the basic-auth credentials are the
standard pattern); ``bearer`` is also supported for setups that use it.

Job path is read from ``IntegrationConfig.resource`` (e.g.
``"my-folder/my-pipeline"``); segments are URL-encoded.

Hard limits:

- Window clamped by ``clamp_window`` (max 7 days, min 60 s).
- ``MAX_PIPELINES_PER_WINDOW = 100`` builds per call.
- Body cap: 5 MiB.
- Auth: basic or bearer. Secrets never logged; scrubbed.

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


class JenkinsCollector(BaseCollector, CICDProvider):
    """Read-only Jenkins collector."""

    name = "jenkins"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "jenkins":
            raise IntegrationError(
                f"JenkinsCollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'jenkins'"
            )
        if not config.resource:
            raise IntegrationError(
                "JenkinsCollector requires IntegrationConfig.resource "
                "(e.g. 'my-folder/my-pipeline')"
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
        return self._fetch_builds(since, until)

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

        runs = self._fetch_builds(start, end)
        for run in runs:
            self._populate_build_detail(run)

        builds = [{"number": int(r.id)} for r in runs]
        envelope = {
            "type": "jenkins_builds",
            "job": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "run_count": len(runs),
            "builds": builds,
        }
        envelope_text = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True
        )

        item = CollectedItem(
            source="jenkins",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "job": cfg.resource,
                "run_count": len(runs),
            },
        )

        meta = {
            "source": "jenkins",
            "endpoint": cfg.endpoint,
            "job": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "run_count": len(runs),
        }
        return [item], meta

    # ------------------------------------------------------------------
    # Transport — build list
    # ------------------------------------------------------------------
    def _fetch_builds(
        self,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[PipelineRun]:
        cfg = self._config
        tree = (
            "builds[number,timestamp,result,url]"
        )
        path = f"/job/{_encode_job_path(cfg.resource)}/api/json"
        url = _join_url(cfg.endpoint, path) + f"?tree={urllib.parse.quote(tree)}"
        raw_text, status = self._http_get(url)
        if status >= 400:
            raise IntegrationError(
                f"JenkinsCollector returned HTTP {status}: "
                f"{_scrub_text(raw_text)[:256]}"
            )
        return self._parse_builds(raw_text, since, until)

    def _parse_builds(
        self,
        raw_text: str,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[PipelineRun]:
        try:
            payload = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"JenkinsCollector received malformed JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            return []
        builds_raw = payload.get("builds") or []
        if not isinstance(builds_raw, list):
            return []
        runs: List[PipelineRun] = []
        for entry in builds_raw[:MAX_PIPELINES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            ts_ms = entry.get("timestamp")
            started_at = (
                _jenkins_ts_to_iso(ts_ms) if isinstance(ts_ms, (int, float)) else None
            )
            if isinstance(started_at, str):
                parsed_started = _parse_iso(started_at)
                if parsed_started is not None and not _in_window(
                    parsed_started, since, until
                ):
                    continue
            runs.append(
                PipelineRun(
                    id=str(entry.get("number") or ""),
                    kind="jenkins",
                    status=str(entry.get("result") or "") or None,
                    name=None,
                    branch=None,
                    commit_sha=None,
                    environment=None,
                    started_at=started_at,
                    finished_at=None,
                    url=(
                        str(entry.get("url") or "")
                        if isinstance(entry.get("url"), str)
                        else None
                    ),
                    actor=None,
                    conclusion=str(entry.get("result") or "") or None,
                    failure_message=None,
                    stages=[],
                )
            )
        return runs

    # ------------------------------------------------------------------
    # Transport — per-build detail
    # ------------------------------------------------------------------
    def _populate_build_detail(self, run: PipelineRun) -> None:
        cfg = self._config
        path = (
            f"/job/{_encode_job_path(cfg.resource)}/{run.id}/api/json"
        )
        url = _join_url(cfg.endpoint, path) + (
            "?tree=number,result,timestamp,builtOn,actions["
            "queuingDurationMillis,totalDurationMillis],changeSet["
            "items[commitId,msg,author[fullName]]]"
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
        # Extract commit info from changeSet (may be empty for periodic builds).
        items = ((payload.get("changeSet") or {}).get("items")) or []
        commit_sha: Optional[str] = None
        actor: Optional[str] = None
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                commit_sha = (
                    str(first.get("commitId") or "") or None
                )
                author = first.get("author") or {}
                if isinstance(author, dict):
                    actor = (
                        str(author.get("fullName") or "") or None
                    )

        finished_at: Optional[str] = None
        ts_ms = payload.get("timestamp")
        if isinstance(ts_ms, (int, float)):
            finished_at = _jenkins_ts_to_iso(ts_ms)

        # Project a single stage entry summarising the build itself.
        failure_message = (
            project_failure_message(payload.get("result"))
            if payload.get("result") in ("FAILURE", "UNSTABLE")
            else None
        )
        stage = PipelineStage(
            name="build",
            status=str(payload.get("result") or "") or None,
            conclusion=str(payload.get("result") or "") or None,
            started_at=run.started_at,
            finished_at=finished_at,
            failure_message=failure_message,
        )

        new_stages: List[PipelineStage] = [stage]
        object.__setattr__(run, "stages", new_stages[:MAX_STAGES_PER_PIPELINE])
        if commit_sha and not run.commit_sha:
            object.__setattr__(run, "commit_sha", commit_sha)
        if actor and not run.actor:
            object.__setattr__(run, "actor", actor)
        if failure_message and not run.failure_message:
            object.__setattr__(run, "failure_message", failure_message)
        if finished_at and not run.finished_at:
            object.__setattr__(run, "finished_at", finished_at)

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
                    f"JenkinsCollector timed out after {timeout}s"
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
                    f"JenkinsCollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"JenkinsCollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"JenkinsCollector network error: {exc}"
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
                f"JenkinsCollector response was not decodable: {exc}"
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
            "User-Agent": "AutoRCA-JenkinsCollector/1.0",
        }
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            if self._config.auth_scheme == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            else:
                # Jenkins accepts ``Basic base64(username:apitoken)``.
                # We treat the resolved secret as the full
                # ``user:apitoken`` string.
                import base64

                encoded = base64.b64encode(
                    secret.encode("utf-8")
                ).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
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
                f"JenkinsCollector response exceeded {max_bytes} bytes"
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


def _encode_job_path(path: str) -> str:
    return urllib.parse.quote(path, safe="")


def _jenkins_ts_to_iso(ms: Any) -> Optional[str]:
    if not isinstance(ms, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(
            float(ms) / 1000.0, tz=dt.timezone.utc
        ).isoformat()
    except (ValueError, OSError):
        return None


def _parse_iso(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _in_window(
    ts: Optional[dt.datetime],
    start: Optional[dt.datetime],
    end: Optional[dt.datetime],
) -> bool:
    if ts is None:
        return True
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


__all__ = [
    "JenkinsCollector",
    "PipelineRun",
    "PipelineStage",
]
