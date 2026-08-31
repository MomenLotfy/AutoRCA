"""
collectors/gitlab_change_collector.py
-----------------------------------------------------------------------------
GitLabChangeCollector — Phase 2.2.

Hits the GitLab REST API over HTTPS using ``urllib.request`` (stdlib
only — no new dependency) to fetch commits and merge requests for a
single project within a bounded time window. Implements
``ChangeProvider`` so the API layer can dispatch to it without
knowing the transport.

Supported endpoints (token-authenticated, ``PRIVATE-TOKEN`` header):

- ``GET /api/v4/projects/{url-encoded project_path}/repository/commits``
  with ``since``/``until`` ISO-8601 parameters
- ``GET /api/v4/projects/{url-encoded project_path}/merge_requests``
  filtered to ``updated_after`` / ``updated_before``

Project path is read from ``IntegrationConfig.resource`` (e.g.
``"mygroup/mysubgroup/myproject"``). The path is URL-encoded
component-wise so it survives ``/`` characters.

The collector is opt-in: constructed only when an investigation
request explicitly provides ``gitlab_changes`` configuration. When
unavailable, ``is_available()`` returns ``False`` and the pipeline is
not affected.

Hard limits: window clamped by ``clamp_window`` (max 7 days); event
count bounded by ``MAX_CHANGES_PER_WINDOW`` (200). Tokens are never
logged and scrubbed from any error message.

The collector never touches ``engine/``, never invents observations,
and never invents root cause.
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
from collectors.change_provider_base import (
    MAX_CHANGES_PER_WINDOW,
    ChangeEvent,
    ChangeProvider,
    clamp_window,
    project_extra,
)
from collectors.integration_base import (
    IntegrationConfig,
    IntegrationError,
    IntegrationResult,
)


logger = logging.getLogger(__name__)

# Hard cap on raw response body.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class GitLabChangeCollector(BaseCollector, ChangeProvider):
    """Collect change events from a GitLab project."""

    name = "gitlab_changes"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "gitlab_changes":
            raise IntegrationError(
                f"GitLabChangeCollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'gitlab_changes'"
            )
        if not config.resource:
            raise IntegrationError(
                "GitLabChangeCollector requires IntegrationConfig.resource "
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
    # ChangeProvider
    # ------------------------------------------------------------------
    def collect_changes(
        self,
        repo: str,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
        ctx: Optional[IncidentContext] = None,
    ) -> List[ChangeEvent]:
        """Provider-neutral entry point. ``repo`` is accepted for
        protocol conformance; this implementation always uses
        ``IntegrationConfig.resource``."""
        events: List[ChangeEvent] = []
        events.extend(self._fetch_commits(since, until))
        events.extend(self._fetch_merge_requests(since, until))
        return events[:MAX_CHANGES_PER_WINDOW]

    # ------------------------------------------------------------------
    # Internal collection
    # ------------------------------------------------------------------
    def _collect_items(
        self, ctx: Optional[IncidentContext] = None
    ) -> Tuple[List[CollectedItem], Dict[str, Any]]:
        cfg = self._config

        # Resolve window.
        start = cfg.incident_start
        end = cfg.incident_end
        if ctx is not None and ctx.is_set():
            start = start or ctx.incident_start
            end = end or ctx.incident_end
        if start is None and end is None:
            raise IntegrationError(
                "GitLabChangeCollector requires a time window "
                "(incident_start / incident_end); unbounded collection "
                "is forbidden"
            )
        start, end = clamp_window(start, end)

        events = self.collect_changes(cfg.resource, start, end, ctx)

        envelope = {
            "type": "gitlab_changes",
            "project": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "event_count": len(events),
            "events": [e.to_dict() for e in events],
        }
        envelope_text = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True
        )

        item = CollectedItem(
            source="gitlab_changes",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "project": cfg.resource,
                "event_count": len(events),
            },
        )

        meta = {
            "source": "gitlab_changes",
            "endpoint": cfg.endpoint,
            "project": cfg.resource,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "event_count": len(events),
        }

        return [item], meta

    # ------------------------------------------------------------------
    # Transport — commits
    # ------------------------------------------------------------------
    def _fetch_commits(
        self,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[ChangeEvent]:
        cfg = self._config
        params: Dict[str, str] = {"per_page": str(min(100, cfg.size))}
        if since is not None:
            params["since"] = _to_gitlab_iso(since)
        if until is not None:
            params["until"] = _to_gitlab_iso(until)

        project_path = _encode_project_path(cfg.resource)
        url = _join_url(
            cfg.endpoint,
            f"/api/v4/projects/{project_path}/repository/commits",
        ) + "?" + _urlencode(params)

        raw_text, status = self._http_get(url)
        if status >= 400:
            raise IntegrationError(
                f"GitLabChangeCollector commits endpoint returned "
                f"HTTP {status}: {_scrub_text(raw_text)[:256]}"
            )
        return self._parse_commits(raw_text)

    def _parse_commits(self, raw_text: str) -> List[ChangeEvent]:
        try:
            payload = json.loads(raw_text) if raw_text else []
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"GitLabChangeCollector received malformed JSON from "
                f"/commits: {exc}"
            ) from exc
        if not isinstance(payload, list):
            return []

        events: List[ChangeEvent] = []
        for entry in payload[:MAX_CHANGES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            author_obj = entry.get("author") or {}
            committer_obj = entry.get("committer") or {}
            title = str(entry.get("title") or "")[:256]
            events.append(
                ChangeEvent(
                    id=str(entry.get("id") or entry.get("short_id") or ""),
                    kind="commit",
                    title=title,
                    author=str(
                        author_obj.get("username")
                        or author_obj.get("name")
                        or ""
                    ) or None,
                    timestamp=str(
                        author_obj.get("date") or committer_obj.get("date") or ""
                    ) or None,
                    url=str(entry.get("web_url") or "") or None,
                    sha=str(entry.get("id") or entry.get("short_id") or "") or None,
                    ref=None,
                    extra={},
                )
            )
        return events

    # ------------------------------------------------------------------
    # Transport — merge requests
    # ------------------------------------------------------------------
    def _fetch_merge_requests(
        self,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[ChangeEvent]:
        cfg = self._config
        params: Dict[str, str] = {
            "state": "closed",
            "order_by": "updated_at",
            "sort": "desc",
            "per_page": str(min(100, cfg.size)),
        }
        if since is not None:
            params["updated_after"] = _to_gitlab_iso(since)
        if until is not None:
            params["updated_before"] = _to_gitlab_iso(until)

        project_path = _encode_project_path(cfg.resource)
        url = _join_url(
            cfg.endpoint,
            f"/api/v4/projects/{project_path}/merge_requests",
        ) + "?" + _urlencode(params)

        raw_text, status = self._http_get(url)
        if status >= 400:
            raise IntegrationError(
                f"GitLabChangeCollector merge_requests endpoint returned "
                f"HTTP {status}: {_scrub_text(raw_text)[:256]}"
            )
        return self._parse_merge_requests(raw_text)

    def _parse_merge_requests(self, raw_text: str) -> List[ChangeEvent]:
        try:
            payload = json.loads(raw_text) if raw_text else []
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"GitLabChangeCollector received malformed JSON from "
                f"/merge_requests: {exc}"
            ) from exc
        if not isinstance(payload, list):
            return []

        events: List[ChangeEvent] = []
        for entry in payload[:MAX_CHANGES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            user_obj = entry.get("author") or {}
            events.append(
                ChangeEvent(
                    id=str(entry.get("iid") or ""),
                    kind="merge_request",
                    title=str(entry.get("title") or "")[:256],
                    author=str(user_obj.get("username") or "") or None,
                    timestamp=str(entry.get("updated_at") or "") or None,
                    url=str(entry.get("web_url") or "") or None,
                    sha=str(entry.get("merge_commit_sha") or "") or None,
                    ref=str(entry.get("source_branch") or "") or None,
                    extra=project_extra(
                        {
                            "state": entry.get("state"),
                            "target_branch": entry.get("target_branch"),
                            "merged": entry.get("state") == "merged",
                        }
                    ),
                )
            )
        return events

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
                    f"GitLabChangeCollector timed out after {timeout}s"
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
                    f"GitLabChangeCollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"GitLabChangeCollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"GitLabChangeCollector network error: {exc}"
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
                f"GitLabChangeCollector response was not decodable: {exc}"
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
            "User-Agent": "AutoRCA-GitLabChangeCollector/1.0",
        }
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            # GitLab uses PRIVATE-TOKEN by default. Bearer is supported
            # via OAuth2; we honour auth_scheme if the caller asked for
            # it explicitly.
            if self._config.auth_scheme == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            else:
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
                f"GitLabChangeCollector response exceeded {max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _scrub_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(
        r"(?i)authorization\s*[:=]\s*\S.*",
        "authorization: ***",
        text,
    )
    text = re.sub(
        r"(?i)private-token\s*[:=]\s*\S.*",
        "private-token: ***",
        text,
    )
    return text[:512]


def _join_url(base: str, suffix: str) -> str:
    base = base.rstrip("/")
    suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    return f"{base}{suffix}"


def _urlencode(params: Dict[str, Any]) -> str:
    return urllib.parse.urlencode(params)


def _encode_project_path(path: str) -> str:
    """GitLab project paths contain ``/`` (groups/subgroups/project).
    URL-encode each segment so the ``/`` becomes ``%2F`` while preserving
    the path separators as required by GitLab's REST API."""
    return urllib.parse.quote(path, safe="")


def _to_gitlab_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "GitLabChangeCollector",
    "ChangeEvent",
    "IntegrationConfig",
    "IntegrationError",
    "IntegrationResult",
]
