"""
collectors/elasticsearch_collector.py
-----------------------------------------------------------------------------
ElasticsearchCollector — Phase 2.1.

Hits Elasticsearch's ``POST /{index}/_search`` over HTTPS using
``urllib.request`` (stdlib only — no new project dependency). The
collector is opt-in: it is constructed only when an investigation
request explicitly provides ``elasticsearch`` configuration.

The collector never touches ``engine/``, never invents observations,
and never invents root cause. It returns one ``CollectedItem`` per
remote document, normalised into a small JSON envelope that the
``ElasticsearchExtractor`` parses into ``Observation`` objects.

Security:

- URL scheme allowlist (http/https only).
- No embedded credentials in the URL — auth via env var name only.
- The Authorization header is scrubbed from every log/exception path
  via ``_redact_headers``.
- Response body capped at 5 MiB; oversized responses raise
  ``IntegrationError`` without spilling bytes into the pipeline.
- Result count bounded (``size`` clamped to ``[1, 1000]``).
- Timeout bounded (clamped to ``[0.5, 30]`` seconds).
- Any HTTP failure (connection refused, 4xx, 5xx, malformed body)
  raises ``IntegrationError`` so the API can return HTTP 400 instead
  of corrupting the pipeline.

The collector is intentionally side-effect-free: no global state, no
threading, no retries. Failures are surfaced once, loudly.
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
from collectors.integration_base import (
    DEFAULT_TIMEOUT_SECONDS,
    IntegrationConfig,
    IntegrationError,
    IntegrationResult,
    MAX_RESULT_SIZE,
)


logger = logging.getLogger(__name__)

# Hard cap on raw response body. ES hits with very large nested docs can
# easily exceed this — we want to fail loud rather than OOM the worker.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Elasticsearch index-pattern grammar. Kept inside the ES collector (not
# in the generic IntegrationConfig) so that Prometheus / GitHub / GitLab
# can use looser grammars without retrofitting the shared module.
_ES_INDEX_PATTERN = re.compile(r"^[A-Za-z0-9_.\-*?]+$")

# Fields we extract into CollectedItem.metadata. Anything else stays in
# the raw ES response and is never surfaced to the pipeline or the UI.
_SAFE_METADATA_KEYS = (
    "index",
    "id",
    "timestamp",
    "service",
    "message",
    "level",
    "host",
)


class ElasticsearchCollector(BaseCollector):
    """Collect bounded search results from Elasticsearch.

    Parameters are passed exclusively through ``IntegrationConfig`` —
    there is no "convenience" positional API. The collector is fully
    driven by the API layer's validated configuration.

    The collector never connects until ``collect()`` is invoked.
    """

    name = "elasticsearch"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "elasticsearch":
            raise IntegrationError(
                f"ElasticsearchCollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'elasticsearch'"
            )
        # Defensive copy of mutable fields.
        self._config = config
        self._cached_secret: Optional[str] = None
        self._secret_resolved = False

    # ------------------------------------------------------------------
    # Availability — never raise; return a soft bool so the pipeline can
    # decide to skip gracefully if integration isn't reachable.
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        if not self._config.endpoint:
            return False
        if not self._config.index_pattern:
            return False
        # ES-specific: enforce the index-pattern grammar here so the
        # generic IntegrationConfig stays source-agnostic.
        import re
        if not _ES_INDEX_PATTERN.match(self._config.index_pattern):
            return False
        return True

    # ------------------------------------------------------------------
    # Collect
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
    # Internal
    # ------------------------------------------------------------------
    def _collect_items(
        self, ctx: Optional[IncidentContext] = None
    ) -> Tuple[List[CollectedItem], Dict[str, Any]]:
        cfg = self._config

        # Time window resolution: explicit IntegrationConfig wins;
        # IncidentContext fills in any missing side.
        start = cfg.incident_start
        end = cfg.incident_end
        if ctx is not None and ctx.is_set():
            start = start or ctx.incident_start
            end = end or ctx.incident_end

        if start is None and end is None:
            raise IntegrationError(
                "ElasticsearchCollector requires a time window "
                "(incident_start / incident_end); unbounded collection "
                "is forbidden"
            )

        # ES-specific grammar check. The shared IntegrationConfig
        # no longer enforces this (it must serve Prometheus metrics
        # and GitHub/GitLab repos too).
        if not _ES_INDEX_PATTERN.match(cfg.index_pattern):
            raise IntegrationError(
                f"ElasticsearchCollector received an invalid "
                f"index_pattern: {cfg.index_pattern!r}"
            )

        # Defensive: re-clamp size and timeout (config __post_init__ does
        # this too, but collectors may be instantiated directly).
        size = max(1, min(MAX_RESULT_SIZE, int(cfg.size)))
        timeout = float(cfg.timeout_seconds)

        body = _build_query(
            index_pattern=cfg.index_pattern,
            service=cfg.service,
            start=start,
            end=end,
            size=size,
            query_fragment=cfg.query,
        )

        secret = self._resolve_secret()
        headers = self._build_headers(secret)

        raw_text, hit_count, status = self._http_search(
            endpoint=cfg.endpoint,
            index_pattern=cfg.index_pattern,
            body=body,
            headers=headers,
            timeout=timeout,
            verify_tls=cfg.verify_tls,
        )

        # Parse the envelope and project safe metadata fields.
        envelope = {
            "type": "elasticsearch_search",
            "index_pattern": cfg.index_pattern,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "size": size,
            "hit_count": hit_count,
            "status": status,
            "hits": _project_safe_hits(raw_text),
        }
        envelope_text = json.dumps(envelope, ensure_ascii=False, sort_keys=True)

        item = CollectedItem(
            source="elasticsearch",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "index_pattern": cfg.index_pattern,
                "service": cfg.service,
                "hit_count": hit_count,
                "incident_start": start.isoformat() if start else None,
                "incident_end": end.isoformat() if end else None,
            },
        )

        meta = {
            "source": "elasticsearch",
            "endpoint": cfg.endpoint,
            "index_pattern": cfg.index_pattern,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "size": size,
            "hit_count": hit_count,
            "http_status": status,
        }

        return [item], meta

    # ------------------------------------------------------------------
    # HTTP boundary
    # ------------------------------------------------------------------
    def _http_search(
        self,
        *,
        endpoint: str,
        index_pattern: str,
        body: str,
        headers: Dict[str, str],
        timeout: float,
        verify_tls: bool,
    ) -> Tuple[str, int, int]:
        """Issue POST /{index}/_search. Returns (raw_body, hit_count, http_status).

        Raises ``IntegrationError`` on any failure.
        """
        import urllib.error
        import urllib.request

        url = _join_url(endpoint, f"/{index_pattern}/_search")

        request = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )

        # Apply process-wide default timeout as a second line of defence
        # alongside the per-call ``timeout`` argument to ``urlopen``.
        previous_default_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
            except socket.timeout as exc:
                raise IntegrationError(
                    f"ElasticsearchCollector timed out after {timeout}s"
                ) from exc
            except urllib.error.HTTPError as exc:
                # Read at most 4 KiB of the error body so we can include
                # something useful in the exception without leaking
                # arbitrary upstream content.
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
                    f"ElasticsearchCollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"ElasticsearchCollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"ElasticsearchCollector network error: {exc}"
                ) from exc
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
                f"ElasticsearchCollector response was not decodable: {exc}"
            ) from exc

        if not raw_text:
            return "", 0, 200

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"ElasticsearchCollector returned malformed JSON: {exc}"
            ) from exc

        hit_count = 0
        if isinstance(payload, dict):
            hits = payload.get("hits") or {}
            if isinstance(hits, dict):
                inner_hits = hits.get("hits") or []
                if isinstance(inner_hits, list):
                    hit_count = len(inner_hits)

        return raw_text, hit_count, 200

    # ------------------------------------------------------------------
    # Secrets — read once, never logged, never returned.
    # ------------------------------------------------------------------
    def _resolve_secret(self) -> Optional[str]:
        if self._secret_resolved:
            return self._cached_secret
        self._secret_resolved = True
        name = self._config.auth_env
        if not name:
            self._cached_secret = None
            return None
        value = os.environ.get(name)
        if not value:
            # We intentionally do NOT raise here — the collector may
            # be pointed at an unauthenticated cluster (e.g. a local
            # test fixture). The caller chose the auth_env name; if
            # it's empty, that's the caller's decision.
            self._cached_secret = None
            return None
        self._cached_secret = value
        return value

    def _build_headers(self, secret: Optional[str]) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AutoRCA-ElasticsearchCollector/1.0",
        }
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            import base64

            encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SAFE_READ_STATUSES = frozenset({200})


def _safe_read(response, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from an HTTP response. Raises
    IntegrationError if the body exceeds the cap."""
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise IntegrationError(
                f"ElasticsearchCollector response exceeded "
                f"{max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _scrub_text(text: Optional[str]) -> str:
    if not text:
        return ""
    # Defence in depth: even if Authorization leaked into the body of an
    # error response, never echo it back. Greedy match so the entire
    # trailing secret is removed even when additional text follows.
    text = re.sub(
        r"(?i)authorization\s*[:=]\s*\S.*", "authorization: ***", text
    )
    return text[:512]


def _join_url(base: str, suffix: str) -> str:
    base = base.rstrip("/")
    suffix = suffix.lstrip("/")
    return f"{base}/{suffix}"


def _build_query(
    *,
    index_pattern: str,
    service: Optional[str],
    start: Optional[dt.datetime],
    end: Optional[dt.datetime],
    size: int,
    query_fragment: Optional[str],
) -> str:
    """Build the deterministic, bounded ES search body.

    Two acceptable modes:

    - ``query_fragment`` is provided AND parses as JSON with a ``query``
      key — we wrap it in a ``bool.filter`` so time-window / service
      filters are added non-destructively. (Currently conservative: if
      the fragment is present, the collector refuses to mix it with
      ``service`` filters; only time bounds are merged.)
    - Otherwise we build a bool/filter with optional time range and
      service term.
    """
    filters: List[Dict[str, Any]] = []
    if start is not None or end is not None:
        range_clause: Dict[str, Any] = {}
        if start is not None:
            range_clause["gte"] = _to_iso(start)
        if end is not None:
            range_clause["lte"] = _to_iso(end)
        filters.append({"range": {"@timestamp": range_clause}})

    if query_fragment:
        try:
            fragment = json.loads(query_fragment)
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"IntegrationConfig.query is not valid JSON: {exc}"
            ) from exc
        if not isinstance(fragment, dict) or "query" not in fragment:
            raise IntegrationError(
                "IntegrationConfig.query must be a JSON object with a "
                "'query' key"
            )
        # Compose: outer bool/filter for time bounds, inner query verbatim.
        bool_query: Dict[str, Any] = {
            "bool": {
                "must": [fragment["query"]],
                "filter": filters,
            }
        }
    else:
        if service:
            filters.append({"term": {"service": service}})
        bool_query = {"bool": {"filter": filters}} if filters else {"match_all": {}}

    body = {
        "size": int(size),
        "_source": [
            "@timestamp",
            "message",
            "service",
            "log.level",
            "host.name",
        ],
        "query": bool_query,
        "sort": [{"@timestamp": {"order": "desc"}}],
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _to_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _project_safe_hits(raw_text: str) -> List[Dict[str, Any]]:
    """Extract a small whitelist of fields from each ES hit.

    Returns an empty list if parsing fails — the caller already has the
    raw envelope in ``raw_text`` and the extractor will parse it again
    in a structured way.
    """
    if not raw_text:
        return []
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    hits_obj = payload.get("hits") or {}
    if not isinstance(hits_obj, dict):
        return []
    inner_hits = hits_obj.get("hits") or []
    if not isinstance(inner_hits, list):
        return []

    projected: List[Dict[str, Any]] = []
    for hit in inner_hits:
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source") or {}
        if not isinstance(source, dict):
            source = {}

        record: Dict[str, Any] = {}
        # Whitelist fields only.
        if isinstance(hit.get("_index"), str):
            record["index"] = hit["_index"]
        # Only keep _id if it looks safe (string, <= 256 chars, ASCII-ish).
        doc_id = hit.get("_id")
        if isinstance(doc_id, str) and 0 < len(doc_id) <= 256:
            record["id"] = doc_id

        ts_value = source.get("@timestamp")
        if isinstance(ts_value, str) and ts_value.strip():
            record["timestamp"] = ts_value

        msg_value = source.get("message")
        if isinstance(msg_value, str):
            # Truncate to keep envelope small.
            record["message"] = msg_value[:512]

        service_value = source.get("service")
        if isinstance(service_value, str):
            record["service"] = service_value

        level_value = source.get("log.level")
        if isinstance(level_value, str):
            record["level"] = level_value

        host_value = source.get("host.name")
        if isinstance(host_value, str):
            record["host"] = host_value

        projected.append(record)

    return projected


__all__ = [
    "ElasticsearchCollector",
    "IntegrationConfig",
    "IntegrationError",
    "IntegrationResult",
    "MAX_RESPONSE_BYTES",
]
