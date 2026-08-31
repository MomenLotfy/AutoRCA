"""
collectors/prometheus_collector.py
-----------------------------------------------------------------------------
PrometheusCollector — Phase 2.2.

Hits a Prometheus HTTP API endpoint over HTTPS using ``urllib.request``
(stdlib only — no new dependency). Supports both:

- **Instant query** — ``GET /api/v1/query?query=…&time=…``
- **Range query**   — ``GET /api/v1/query_range?query=…&start=…&end=…&step=…``

The collector is opt-in: it is constructed only when an investigation
request explicitly provides ``prometheus`` configuration.

Hard limits (per user direction):

- Time window: 6 hours max (``MAX_WINDOW_SECONDS`` from ``integration_base``).
- Step (range queries): minimum 30s.
- Max samples: bounded — keeps a runaway query from returning millions of
  series even when the server is happy to provide them.

Like the Elasticsearch collector, this module is intentionally
side-effect-free: no global state, no threading, no retries. Failures
are surfaced once, loudly via ``IntegrationError``.

The collector never touches ``engine/``, never invents observations,
and never invents root cause. It returns one ``CollectedItem`` per
query response, normalised into a small JSON envelope that the
``PrometheusExtractor`` parses into ``Observation`` objects.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
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


import os  # noqa: E402


logger = logging.getLogger(__name__)

# Hard cap on raw response body. Prometheus may emit large JSON envelopes
# for many-step range queries.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Hard caps for Prometheus-specific knobs.
MIN_STEP_SECONDS = 30              # user direction: 30s minimum step
MAX_RANGE_SAMPLES = 2000           # total sample budget across all series

# Prometheus metric name grammar (subset of the official spec):
# letters, digits, underscore, colon.
_METRIC_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")


class PrometheusCollector(BaseCollector):
    """Collect bounded query results from a Prometheus HTTP API.

    Parameters are passed exclusively through ``IntegrationConfig``.
    The collector never connects until ``collect()`` is invoked.

    The Prometheus ``PromQL`` query itself is taken from
    ``IntegrationConfig.query`` (free-form) — this is the source of
    truth for what to ask the server. ``index_pattern`` is treated as
    the metric name to assert (if provided) so the user cannot
    accidentally delete an entire cluster's data by typing a wildcard
    into a free-form PromQL string.
    """

    name = "prometheus"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "prometheus":
            raise IntegrationError(
                f"PrometheusCollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'prometheus'"
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
        if not self._config.query:
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

        if not cfg.query:
            raise IntegrationError(
                "PrometheusCollector requires IntegrationConfig.query "
                "(a non-empty PromQL expression)"
            )

        # Time window resolution: explicit IntegrationConfig wins;
        # IncidentContext fills in any missing side.
        start = cfg.incident_start
        end = cfg.incident_end
        if ctx is not None and ctx.is_set():
            start = start or ctx.incident_start
            end = end or ctx.incident_end

        # Range queries need a window. Instant queries also need a
        # single timestamp.
        if start is None and end is None:
            raise IntegrationError(
                "PrometheusCollector requires a time window "
                "(incident_start / incident_end); unbounded collection "
                "is forbidden"
            )

        # Defensive: re-clamp size and timeout.
        size = max(1, min(MAX_RESULT_SIZE, int(cfg.size)))
        timeout = float(cfg.timeout_seconds)

        # Decide query mode: range when both bounds exist; otherwise
        # fall back to instant at the available bound.
        if start is not None and end is not None:
            mode = "range"
            step_seconds = _resolve_step(start, end, cfg.extra_headers)
            url = self._build_range_url(cfg, start, end, step_seconds)
        else:
            mode = "instant"
            url = self._build_instant_url(cfg, start or end)  # one of the two

        # Optional metric-name assertion — surface misconfiguration
        # before we make the network call.
        if cfg.index_pattern:
            if not _METRIC_NAME.match(cfg.index_pattern):
                raise IntegrationError(
                    f"PrometheusCollector received an invalid metric "
                    f"name in index_pattern: {cfg.index_pattern!r}"
                )

        secret = self._resolve_secret()
        headers = self._build_headers(secret)

        raw_text, status, sample_count, series_count = self._http_query(
            url=url,
            headers=headers,
            timeout=timeout,
            verify_tls=cfg.verify_tls,
        )

        # Cap the number of samples we keep in the envelope. The full
        # raw text remains in raw_text; the projection is a *summary*
        # of what came back.
        projected = _project_safe_series(
            raw_text,
            max_samples=min(MAX_RANGE_SAMPLES, size * 20),
        )

        envelope = {
            "type": "prometheus_query",
            "mode": mode,
            "query": cfg.query,
            "metric_name": cfg.index_pattern,
            "service": cfg.service,
            "resource": cfg.resource,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "step_seconds": step_seconds if mode == "range" else None,
            "size": size,
            "status": status,
            "sample_count": sample_count,
            "series_count": series_count,
            "series": projected,
        }
        envelope_text = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True
        )

        item = CollectedItem(
            source="prometheus",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "query": cfg.query,
                "mode": mode,
                "metric_name": cfg.index_pattern,
                "service": cfg.service,
                "sample_count": sample_count,
                "series_count": series_count,
                "incident_start": start.isoformat() if start else None,
                "incident_end": end.isoformat() if end else None,
            },
        )

        meta = {
            "source": "prometheus",
            "endpoint": cfg.endpoint,
            "query": cfg.query,
            "metric_name": cfg.index_pattern,
            "mode": mode,
            "service": cfg.service,
            "resource": cfg.resource,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "step_seconds": step_seconds if mode == "range" else None,
            "size": size,
            "sample_count": sample_count,
            "series_count": series_count,
            "http_status": status,
        }

        return [item], meta

    # ------------------------------------------------------------------
    # URL builders
    # ------------------------------------------------------------------
    def _build_range_url(
        self,
        cfg: IntegrationConfig,
        start: dt.datetime,
        end: dt.datetime,
        step_seconds: int,
    ) -> str:
        params = {
            "query": cfg.query,
            "start": _to_unix(start),
            "end": _to_unix(end),
            "step": int(step_seconds),
        }
        return _join_url(cfg.endpoint, "/api/v1/query_range") + "?" + _urlencode(params)

    def _build_instant_url(
        self,
        cfg: IntegrationConfig,
        when: Optional[dt.datetime],
    ) -> str:
        params: Dict[str, str] = {"query": cfg.query}
        if when is not None:
            params["time"] = _to_unix(when)
        return _join_url(cfg.endpoint, "/api/v1/query") + "?" + _urlencode(params)

    # ------------------------------------------------------------------
    # HTTP boundary
    # ------------------------------------------------------------------
    def _http_query(
        self,
        *,
        url: str,
        headers: Dict[str, str],
        timeout: float,
        verify_tls: bool,
    ) -> Tuple[str, int, int, int]:
        """Issue the GET request. Returns (raw_body, status, sample_count, series_count).

        Raises ``IntegrationError`` on any failure.
        """
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=headers, method="GET")

        previous_default_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
            except socket.timeout as exc:
                raise IntegrationError(
                    f"PrometheusCollector timed out after {timeout}s"
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
                    f"PrometheusCollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"PrometheusCollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"PrometheusCollector network error: {exc}"
                ) from exc
            status = getattr(response, "status", 200) or 200
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
                f"PrometheusCollector response was not decodable: {exc}"
            ) from exc

        if not raw_text:
            return "", 0, 200, 0

        # Validate JSON shape early so the caller gets a loud failure
        # for malformed bodies (rather than a silently-empty envelope).
        try:
            json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"PrometheusCollector returned malformed JSON: {exc}"
            ) from exc

        sample_count, series_count = _count_samples(raw_text)
        return raw_text, int(status), sample_count, series_count

    # ------------------------------------------------------------------
    # Secrets
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
            self._cached_secret = None
            return None
        self._cached_secret = value
        return value

    def _build_headers(self, secret: Optional[str]) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "AutoRCA-PrometheusCollector/1.0",
        }
        # Apply user extra_headers (Authorization overrides blocked at
        # the API boundary — see api/security.py).
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            if self._config.auth_scheme == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            elif self._config.auth_scheme == "header":
                # Prometheus native convention uses X-Api-Key for some
                # frontends; we put the secret there.
                headers["X-Api-Key"] = secret
            else:
                import base64

                encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
        return headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_step(
    start: dt.datetime,
    end: dt.datetime,
    extra_headers: Dict[str, str],
) -> int:
    """Compute the range-query step in seconds.

    Honour ``MIN_STEP_SECONDS`` as a floor. The ceiling is the full
    window in seconds (one sample at start, one at end). A caller may
    override the step through ``extra_headers["x-prometheus-step"]``
    but it is clamped to ``MIN_STEP_SECONDS``.
    """
    delta = max(1, int((end - start).total_seconds()))
    override = None
    try:
        if "x-prometheus-step" in extra_headers:
            override = int(extra_headers["x-prometheus-step"])
    except (TypeError, ValueError):
        override = None
    if override is None or override < MIN_STEP_SECONDS:
        override = max(MIN_STEP_SECONDS, delta // 240)  # up to 240 steps
        # Ensure we get at least one data point within 6h.
        if delta <= MIN_STEP_SECONDS:
            override = delta
    return max(MIN_STEP_SECONDS, min(delta, int(override)))


def _to_unix(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return str(value.astimezone(dt.timezone.utc).timestamp())


def _to_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


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
                f"PrometheusCollector response exceeded "
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
    suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    return f"{base}{suffix}"


def _urlencode(params: Dict[str, Any]) -> str:
    """Tiny stdlib urlencode replacement to avoid importing urllib.parse
    in two places."""
    import urllib.parse

    return urllib.parse.urlencode(params)


def _count_samples(raw_text: str) -> Tuple[int, int]:
    """Return (sample_count, series_count) parsed from a Prometheus
    response envelope. Best-effort: returns (0, 0) on any failure."""
    if not raw_text:
        return 0, 0
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return 0, 0
    if not isinstance(payload, dict):
        return 0, 0
    data = payload.get("data") or {}
    result_type = data.get("resultType") if isinstance(data, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        return 0, 0
    series_count = len(result)
    sample_count = 0
    if result_type == "matrix":
        for series in result:
            if not isinstance(series, dict):
                continue
            values = series.get("values") or []
            if isinstance(values, list):
                sample_count += len(values)
    elif result_type == "vector":
        sample_count = series_count
    elif result_type == "scalar":
        sample_count = 1 if result else 0
    return sample_count, series_count


def _project_safe_series(
    raw_text: str, *, max_samples: int
) -> List[Dict[str, Any]]:
    """Project a small whitelist of series data from the Prometheus
    response for the envelope. The full body is also retained in
    ``raw_text`` for the extractor."""
    if not raw_text:
        return []
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return []
    result_type = data.get("resultType")
    result = data.get("result") or []
    if not isinstance(result, list):
        return []

    projected: List[Dict[str, Any]] = []
    samples_kept = 0
    for series in result:
        if not isinstance(series, dict):
            continue
        metric = series.get("metric") or {}
        if not isinstance(metric, dict):
            metric = {}
        # Whitelist a few well-known labels.
        safe_labels = {
            k: str(v)[:128]
            for k, v in metric.items()
            if isinstance(k, str)
            and isinstance(v, (str, int, float, bool))
        }
        record: Dict[str, Any] = {"labels": safe_labels}
        if result_type == "matrix":
            values = series.get("values") or []
            if not isinstance(values, list):
                values = []
            sample_subset: List[List[Any]] = []
            for entry in values:
                if samples_kept >= max_samples:
                    break
                if isinstance(entry, list) and len(entry) >= 2:
                    ts = _safe_float(entry[0])
                    val = _safe_float(entry[1])
                    if ts is None or val is None:
                        continue
                    sample_subset.append([ts, val])
                    samples_kept += 1
            record["samples"] = sample_subset
        elif result_type == "vector":
            value = series.get("value")
            if isinstance(value, list) and len(value) >= 2:
                ts = _safe_float(value[0])
                val = _safe_float(value[1])
                if ts is not None and val is not None:
                    record["value"] = [ts, val]
                    samples_kept += 1
        projected.append(record)
        if samples_kept >= max_samples:
            break
    return projected


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "PrometheusCollector",
    "IntegrationConfig",
    "IntegrationError",
    "IntegrationResult",
    "MAX_RESPONSE_BYTES",
    "MIN_STEP_SECONDS",
    "MAX_RANGE_SAMPLES",
]
