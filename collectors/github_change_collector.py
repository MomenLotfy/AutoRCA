"""
collectors/github_change_collector.py
-----------------------------------------------------------------------------
GitHubChangeCollector — Phase 2.2.

Hits the GitHub REST API over HTTPS using ``urllib.request`` (stdlib
only — no new dependency) to fetch commits, pull requests, and
deployment events for a single repository within a bounded time
window. Implements ``ChangeProvider`` so the API layer can dispatch to
it without knowing the transport.

Supported endpoints (bearer-authenticated):

- ``GET /repos/{owner}/{repo}/commits?since=…&until=…&per_page=…``
- ``GET /repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc``
  (filtered to the requested window)

The collector is opt-in: constructed only when an investigation
request explicitly provides ``github_changes`` configuration. When
unavailable, ``is_available()`` returns ``False`` and the pipeline is
not affected.

Hard limits (per user direction):

- Window: bounded by ``clamp_window`` (max 7 days, min 60s).
- Result count: at most ``MAX_CHANGES_PER_WINDOW`` events.
- Auth via env var (bearer or basic). Tokens are never logged and
  scrubbed from any error message.

The collector never touches ``engine/``, never invents observations,
and never invents root cause. It returns ``CollectedItem`` instances
that ``GitHubChangeExtractor`` parses into ``Observation`` objects.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import socket
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

# Hard cap on raw response body. The GitHub commits endpoint can return
# fairly large payloads for a busy repo.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class GitHubChangeCollector(BaseCollector, ChangeProvider):
    """Collect change events from a GitHub repository.

    The repository is read from ``IntegrationConfig.resource``
    (e.g. ``octocat/Hello-World``). The time window comes from the
    ``incident_start`` / ``incident_end`` fields. The collector emits
    one ``CollectedItem`` per GitHub call (commits, PRs) rather than
    one per record; the extractor fans that envelope out into
    individual ``Observation`` rows.
    """

    name = "github_changes"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "github_changes":
            raise IntegrationError(
                f"GitHubChangeCollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'github_changes'"
            )
        if not config.resource:
            raise IntegrationError(
                "GitHubChangeCollector requires IntegrationConfig.resource "
                "(e.g. 'octocat/Hello-World')"
            )
        self._config = config
        self._cached_secret: Optional[str] = None
        self._secret_resolved = False

    # ------------------------------------------------------------------
    # Availability — never raise; return a soft bool.
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
        """Provider-neutral entry point. The ``repo`` argument is
        accepted for protocol conformance; this implementation always
        uses ``IntegrationConfig.resource`` (validated at construction)."""
        events: List[ChangeEvent] = []
        events.extend(self._fetch_commits(since, until))
        events.extend(self._fetch_pull_requests(since, until))
        # Truncate to the per-window cap.
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
                "GitHubChangeCollector requires a time window "
                "(incident_start / incident_end); unbounded collection "
                "is forbidden"
            )
        start, end = clamp_window(start, end)

        events = self.collect_changes(cfg.resource, start, end, ctx)

        envelope = {
            "type": "github_changes",
            "repo": cfg.resource,
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
            source="github_changes",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "repo": cfg.resource,
                "event_count": len(events),
            },
        )

        meta = {
            "source": "github_changes",
            "endpoint": cfg.endpoint,
            "repo": cfg.resource,
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
            params["since"] = _to_github_iso(since)
        if until is not None:
            # The ``until`` parameter on the commits endpoint is an
            # exclusive upper bound; we add one second to make the
            # window inclusive.
            params["until"] = _to_github_iso(until + dt.timedelta(seconds=1))
        url = _join_url(
            cfg.endpoint, f"/repos/{cfg.resource}/commits"
        ) + "?" + _urlencode(params)

        raw_text, status = self._http_get(url)
        if status >= 400:
            raise IntegrationError(
                f"GitHubChangeCollector commits endpoint returned "
                f"HTTP {status}: {_scrub_text(raw_text)[:256]}"
            )
        return self._parse_commits(raw_text)

    def _parse_commits(self, raw_text: str) -> List[ChangeEvent]:
        try:
            payload = json.loads(raw_text) if raw_text else []
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"GitHubChangeCollector received malformed JSON from "
                f"/commits: {exc}"
            ) from exc
        if not isinstance(payload, list):
            return []

        events: List[ChangeEvent] = []
        for entry in payload[:MAX_CHANGES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            sha = entry.get("sha")
            commit_obj = entry.get("commit") or {}
            author_obj = commit_obj.get("author") or {}
            committer_obj = commit_obj.get("committer") or {}
            user_obj = entry.get("author") or {}
            message = commit_obj.get("message") or ""
            title = message.splitlines()[0] if message else ""
            events.append(
                ChangeEvent(
                    id=str(sha or ""),
                    kind="commit",
                    title=str(title)[:256],
                    author=str(user_obj.get("login") or author_obj.get("name") or "") or None,
                    timestamp=str(author_obj.get("date") or committer_obj.get("date") or "") or None,
                    url=str(entry.get("html_url") or "") or None,
                    sha=str(sha or "") or None,
                    ref=None,
                    extra={},
                )
            )
        return events

    # ------------------------------------------------------------------
    # Transport — pull requests
    # ------------------------------------------------------------------
    def _fetch_pull_requests(
        self,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[ChangeEvent]:
        cfg = self._config
        params: Dict[str, str] = {
            "state": "closed",
            "sort": "updated",
            "direction": "desc",
            "per_page": str(min(100, cfg.size)),
        }
        url = _join_url(
            cfg.endpoint, f"/repos/{cfg.resource}/pulls"
        ) + "?" + _urlencode(params)

        raw_text, status = self._http_get(url)
        if status >= 400:
            raise IntegrationError(
                f"GitHubChangeCollector pulls endpoint returned "
                f"HTTP {status}: {_scrub_text(raw_text)[:256]}"
            )
        return self._parse_pull_requests(raw_text, since, until)

    def _parse_pull_requests(
        self,
        raw_text: str,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
    ) -> List[ChangeEvent]:
        try:
            payload = json.loads(raw_text) if raw_text else []
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"GitHubChangeCollector received malformed JSON from "
                f"/pulls: {exc}"
            ) from exc
        if not isinstance(payload, list):
            return []

        events: List[ChangeEvent] = []
        for entry in payload[:MAX_CHANGES_PER_WINDOW]:
            if not isinstance(entry, dict):
                continue
            updated_at = entry.get("updated_at") or entry.get("closed_at")
            ts = _parse_iso(updated_at) if updated_at else None
            if not _in_window(ts, since, until):
                continue
            user_obj = entry.get("user") or {}
            events.append(
                ChangeEvent(
                    id=str(entry.get("number") or ""),
                    kind="pr",
                    title=str(entry.get("title") or "")[:256],
                    author=str(user_obj.get("login") or "") or None,
                    timestamp=str(updated_at or "") or None,
                    url=str(entry.get("html_url") or "") or None,
                    sha=str(
                        (entry.get("merge_commit_sha") or "") or ""
                    ) or None,
                    ref=str(entry.get("head", {}).get("ref") or "") or None,
                    extra=project_extra(
                        {
                            "merged": bool(entry.get("merged_at")),
                            "state": entry.get("state"),
                            "target_branch": (entry.get("base") or {}).get("ref"),
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
                    f"GitHubChangeCollector timed out after {timeout}s"
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
                    f"GitHubChangeCollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"GitHubChangeCollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"GitHubChangeCollector network error: {exc}"
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
                f"GitHubChangeCollector response was not decodable: {exc}"
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
            "User-Agent": "AutoRCA-GitHubChangeCollector/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            if self._config.auth_scheme == "basic":
                import base64

                encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
            else:
                # Default to bearer (the GitHub-recommended form).
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
                f"GitHubChangeCollector response exceeded {max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _scrub_text(text: Optional[str]) -> str:
    if not text:
        return ""
    # Redact the Authorization header value when echoed in an error body.
    # Use a greedy match so the entire trailing secret is removed even
    # when the body has additional text on the same line.
    text = re.sub(
        r"(?i)authorization\s*[:=]\s*\S.*",
        "authorization: ***",
        text,
    )
    # Redact any "Bearer <token>" pattern (GitHub bearer tokens).
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._\-]+",
        "bearer ***",
        text,
    )
    # Redact "private-token: <token>" / "token: <token>" patterns.
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
    import urllib.parse

    return urllib.parse.urlencode(params)


def _to_github_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    # GitHub's commits endpoint accepts RFC3339 with or without
    # sub-second precision; use the standard form.
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str):
        return None
    try:
        # Handle GitHub's "Z" suffix.
        cleaned = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _in_window(
    ts: Optional[dt.datetime],
    since: Optional[dt.datetime],
    until: Optional[dt.datetime],
) -> bool:
    if ts is None:
        # Be permissive — GitHub sometimes returns null timestamps.
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


__all__ = [
    "GitHubChangeCollector",
    "ChangeEvent",
    "IntegrationConfig",
    "IntegrationError",
    "IntegrationResult",
]
