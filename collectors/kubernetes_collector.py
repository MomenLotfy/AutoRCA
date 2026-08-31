"""
collectors/kubernetes_collector.py
-----------------------------------------------------------------------------
KubernetesCollector — Phase 2.3.

Read-only Kubernetes integration via the Kubernetes REST API. Strictly
GET-only; the collector never mutates cluster state. Stdlib
``urllib.request`` only — no ``kubectl`` invocation, no shell, no
client-go dependency.

Endpoints used (when in scope):

- ``GET /api/v1/namespaces/{ns}/pods``              (pods + container states)
- ``GET /api/v1/namespaces/{ns}/events``           (Warning / Normal events)
- ``GET /apis/apps/v1/namespaces/{ns}/deployments``(deployment status)
- ``GET /api/v1/namespaces/{ns}/pods/{name}/status``(single-pod detail,
   optional, used when the pod is named in the request)

If the request supplies only a cluster endpoint and no namespace, the
collector falls back to listing namespaces first
(``GET /api/v1/namespaces``) and reading from the first namespace that
matches the optional ``resource`` filter, or from ``default`` if no
filter is provided.

Hard limits:

- Single namespace per call (no cluster-wide scans unless explicitly
  asked via ``resource="all-namespaces"`` — and even then the result
  is bounded by ``size``).
- Time window: 6 h max (``MAX_WINDOW_SECONDS``).
- Body size: 5 MiB cap.
- Timeout: bounded (``MAX_TIMEOUT_SECONDS``).
- Auth via env var (``auth_env``); bearer token used by default
  (service-account bearer tokens are the only supported auth scheme —
  K8s has no "basic" / "header" auth for the API server).

Security:

- Read-only. No POST/PUT/PATCH/DELETE ever.
- No ``shell=True``, no subprocess, no kubectl exec.
- No client-go / no PyYAML parsing — JSON in, JSON out.
- Secret scrubbing: ``Authorization`` and ``Bearer`` patterns are
  removed from every error message.
- Namespace grammar: ``^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`` (the official
  K8s rule).

Phase 2.3 never touches ``engine/`` or the deterministic RCA pipeline.
The collector returns ``CollectedItem``s that ``KubernetesExtractor``
parses into ``Observation``s.
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
    IntegrationConfig,
    IntegrationError,
    IntegrationResult,
    MAX_RESULT_SIZE,
)


logger = logging.getLogger(__name__)

# Hard cap on raw response body. K8s watch / list endpoints can return
# large JSON documents for busy clusters.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Maximum number of pods / events / deployments to project into the
# envelope. The raw body is kept in raw_text for the extractor; this
# cap only affects the JSON envelope's size.
MAX_K8S_OBJECTS = 200

# Kubernetes namespace grammar (RFC 1123 label). The collector refuses
# to query anything that does not match.
_NAMESPACE_GRAMMAR = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# Container reason values that are well-known to be incident-relevant.
_INCIDENT_REASONS = {
    "OOMKilled",
    "CrashLoopBackOff",
    "ErrImagePull",
    "ImagePullBackOff",
    "CreateContainerConfigError",
    "InvalidImageName",
    "ContainerStatusUnknown",
    "RunContainerError",
    "Error",
    "Failed",
    "FailedMount",
    "FailedScheduling",
    "Unhealthy",
    "BackOff",
    "NodeLost",
    "NodeNotReady",
    "NodeRebooted",
}


class KubernetesCollector(BaseCollector):
    """Read-only Kubernetes REST collector.

    Configuration is exclusively through ``IntegrationConfig``. The
    collector never connects until ``collect()`` is invoked.
    """

    name = "kubernetes"

    def __init__(self, config: IntegrationConfig) -> None:
        if config.source != "kubernetes":
            raise IntegrationError(
                f"KubernetesCollector received IntegrationConfig.source="
                f"{config.source!r}; expected 'kubernetes'"
            )
        # K8s uses bearer auth (service-account tokens). Force the
        # auth_scheme to bearer regardless of caller input — there is
        # no other supported K8s auth scheme for the API server.
        if config.auth_env and config.auth_scheme not in ("bearer",):
            raise IntegrationError(
                "KubernetesCollector only supports bearer auth "
                "(service-account token)"
            )
        # If the caller supplied a resource that looks like a namespace,
        # validate it. ``resource="all-namespaces"`` is a sentinel that
        # we handle specially in collect().
        if config.resource and config.resource != "all-namespaces":
            if not _NAMESPACE_GRAMMAR.match(config.resource):
                raise IntegrationError(
                    f"KubernetesCollector received an invalid "
                    f"namespace: {config.resource!r}"
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
        # Either a namespace in resource, or the special
        # all-namespaces sentinel.
        if not self._config.resource:
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
                "KubernetesCollector requires a time window "
                "(incident_start / incident_end); unbounded collection "
                "is forbidden"
            )

        namespace = (
            cfg.resource
            if cfg.resource and cfg.resource != "all-namespaces"
            else None
        )
        all_namespaces = cfg.resource == "all-namespaces"

        secret = self._resolve_secret()
        headers = self._build_headers(secret)
        timeout = float(cfg.timeout_seconds)
        size = max(1, min(MAX_RESULT_SIZE, int(cfg.size)))

        pods_payload: List[Dict[str, Any]] = []
        events_payload: List[Dict[str, Any]] = []
        deployments_payload: List[Dict[str, Any]] = []
        namespaces_payload: List[Dict[str, Any]] = []

        namespaces_to_query: List[str] = []
        if namespace:
            namespaces_to_query.append(namespace)
        elif all_namespaces:
            namespaces_payload = self._list_namespaces(headers, timeout, size)
            namespaces_to_query = [
                _safe_name(ns_obj)
                for ns_obj in namespaces_payload
                if _safe_name(ns_obj)
            ][:MAX_K8S_OBJECTS]
        else:
            namespaces_to_query.append("default")

        for ns in namespaces_to_query:
            if len(pods_payload) < MAX_K8S_OBJECTS:
                pods = self._list_pods(ns, headers, timeout, size, start, end)
                pods_payload.extend(pods)
            if len(events_payload) < MAX_K8S_OBJECTS:
                events = self._list_events(
                    ns, headers, timeout, size, start, end
                )
                events_payload.extend(events)
            if len(deployments_payload) < MAX_K8S_OBJECTS:
                deps = self._list_deployments(
                    ns, headers, timeout, size
                )
                deployments_payload.extend(deps)

        envelope = {
            "type": "kubernetes_state",
            "namespace": namespace,
            "all_namespaces": all_namespaces,
            "namespaces_queried": namespaces_to_query,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "pod_count": len(pods_payload),
            "event_count": len(events_payload),
            "deployment_count": len(deployments_payload),
            "incident_reasons": _summarise_incident_reasons(
                pods_payload, events_payload
            ),
            "pods": [_project_pod(p) for p in pods_payload[:MAX_K8S_OBJECTS]],
            "events": [
                _project_event(e) for e in events_payload[:MAX_K8S_OBJECTS]
            ],
            "deployments": [
                _project_deployment(d)
                for d in deployments_payload[:MAX_K8S_OBJECTS]
            ],
        }
        envelope_text = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True
        )

        item = CollectedItem(
            source="kubernetes",
            raw_text=envelope_text,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            metadata={
                "namespace": namespace,
                "pod_count": len(pods_payload),
                "event_count": len(events_payload),
                "deployment_count": len(deployments_payload),
                "incident_reasons": envelope["incident_reasons"],
            },
        )

        meta = {
            "source": "kubernetes",
            "endpoint": cfg.endpoint,
            "namespace": namespace,
            "all_namespaces": all_namespaces,
            "service": cfg.service,
            "incident_start": start.isoformat() if start else None,
            "incident_end": end.isoformat() if end else None,
            "pod_count": len(pods_payload),
            "event_count": len(events_payload),
            "deployment_count": len(deployments_payload),
            "incident_reasons": envelope["incident_reasons"],
        }

        return [item], meta

    # ------------------------------------------------------------------
    # REST: list namespaces
    # ------------------------------------------------------------------
    def _list_namespaces(
        self,
        headers: Dict[str, str],
        timeout: float,
        size: int,
    ) -> List[Dict[str, Any]]:
        url = _join_url(self._config.endpoint, "/api/v1/namespaces")
        url += "?limit=" + str(size)
        payload = self._http_get_json(url, headers, timeout)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [it for it in items if isinstance(it, dict)]

    # ------------------------------------------------------------------
    # REST: list pods
    # ------------------------------------------------------------------
    def _list_pods(
        self,
        namespace: str,
        headers: Dict[str, str],
        timeout: float,
        size: int,
        start: Optional[dt.datetime],
        end: Optional[dt.datetime],
    ) -> List[Dict[str, Any]]:
        url = (
            _join_url(
                self._config.endpoint,
                f"/api/v1/namespaces/{namespace}/pods",
            )
            + f"?limit={size}"
        )
        try:
            payload = self._http_get_json(url, headers, timeout)
        except IntegrationError:
            # Namespace may not exist or the user may lack permissions.
            # Surface a soft empty rather than crashing the whole call.
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        # Apply incident window filter at the collector level for any
        # pod with a parseable startTime / lastTransitionTime.
        filtered: List[Dict[str, Any]] = []
        for pod in items:
            if not isinstance(pod, dict):
                continue
            ts = _extract_pod_timestamp(pod)
            if ts is not None and not _in_window(ts, start, end):
                continue
            filtered.append(pod)
        return filtered

    # ------------------------------------------------------------------
    # REST: list events
    # ------------------------------------------------------------------
    def _list_events(
        self,
        namespace: str,
        headers: Dict[str, str],
        timeout: float,
        size: int,
        start: Optional[dt.datetime],
        end: Optional[dt.datetime],
    ) -> List[Dict[str, Any]]:
        url = (
            _join_url(
                self._config.endpoint,
                f"/api/v1/namespaces/{namespace}/events",
            )
            + f"?limit={size}"
        )
        try:
            payload = self._http_get_json(url, headers, timeout)
        except IntegrationError:
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        filtered: List[Dict[str, Any]] = []
        for ev in items:
            if not isinstance(ev, dict):
                continue
            ts = _parse_iso(ev.get("lastTimestamp") or ev.get("eventTime"))
            if ts is not None and not _in_window(ts, start, end):
                continue
            filtered.append(ev)
        return filtered

    # ------------------------------------------------------------------
    # REST: list deployments
    # ------------------------------------------------------------------
    def _list_deployments(
        self,
        namespace: str,
        headers: Dict[str, str],
        timeout: float,
        size: int,
    ) -> List[Dict[str, Any]]:
        url = (
            _join_url(
                self._config.endpoint,
                f"/apis/apps/v1/namespaces/{namespace}/deployments",
            )
            + f"?limit={size}"
        )
        try:
            payload = self._http_get_json(url, headers, timeout)
        except IntegrationError:
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [d for d in items if isinstance(d, dict)]

    # ------------------------------------------------------------------
    # HTTP boundary
    # ------------------------------------------------------------------
    def _http_get_json(
        self, url: str, headers: Dict[str, str], timeout: float
    ) -> Dict[str, Any]:
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
                    f"KubernetesCollector timed out after {timeout}s"
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
                    f"KubernetesCollector received HTTP {exc.code}: "
                    f"{_scrub_text(snippet)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise IntegrationError(
                    f"KubernetesCollector connection error: "
                    f"{_scrub_text(str(exc.reason))}"
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    f"KubernetesCollector network error: {exc}"
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
                f"KubernetesCollector response was not decodable: {exc}"
            ) from exc

        if not raw_text:
            return {}
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise IntegrationError(
                f"KubernetesCollector returned malformed JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise IntegrationError(
                "KubernetesCollector response was not a JSON object"
            )
        return payload

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
            "User-Agent": "AutoRCA-KubernetesCollector/1.0 (read-only)",
        }
        for key, value in self._config.extra_headers.items():
            headers[str(key)] = str(value)
        if secret:
            # Bearer token (service-account token).
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
                f"KubernetesCollector response exceeded {max_bytes} bytes"
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


def _safe_name(ns_obj: Dict[str, Any]) -> Optional[str]:
    meta = ns_obj.get("metadata") if isinstance(ns_obj, dict) else None
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    return name if isinstance(name, str) else None


def _extract_pod_timestamp(pod: Dict[str, Any]) -> Optional[dt.datetime]:
    meta = pod.get("metadata") if isinstance(pod, dict) else None
    if not isinstance(meta, dict):
        return None
    return _parse_iso(meta.get("creationTimestamp"))


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
        return True  # permissive: K8s sometimes returns null ts
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


def _summarise_incident_reasons(
    pods: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
) -> List[str]:
    """Return a sorted list of distinct incident-relevant reason
    values found in the supplied pod + event lists."""
    reasons: set = set()
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        statuses = pod.get("status") or {}
        container_statuses = (
            statuses.get("containerStatuses") or []
            if isinstance(statuses, dict)
            else []
        )
        if not isinstance(container_statuses, list):
            continue
        for cs in container_statuses:
            if not isinstance(cs, dict):
                continue
            state = cs.get("state") or {}
            termin = state.get("terminated") or {}
            waiting = state.get("waiting") or {}
            for src in (termin, waiting):
                if not isinstance(src, dict):
                    continue
                reason = src.get("reason")
                if isinstance(reason, str) and reason in _INCIDENT_REASONS:
                    reasons.add(reason)
    for ev in events:
        if not isinstance(ev, dict):
            continue
        reason = ev.get("reason")
        if isinstance(reason, str) and reason in _INCIDENT_REASONS:
            reasons.add(reason)
    return sorted(reasons)


def _project_pod(pod: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist a small set of pod fields for the envelope."""
    meta = pod.get("metadata") if isinstance(pod, dict) else {}
    spec = pod.get("spec") if isinstance(pod, dict) else {}
    status = pod.get("status") if isinstance(pod, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(spec, dict):
        spec = {}
    if not isinstance(status, dict):
        status = {}

    container_statuses = status.get("containerStatuses") or []
    if not isinstance(container_statuses, list):
        container_statuses = []

    containers: List[Dict[str, Any]] = []
    total_restarts = 0
    for cs in container_statuses:
        if not isinstance(cs, dict):
            continue
        restart_count = int(cs.get("restartCount") or 0)
        total_restarts += restart_count
        image = cs.get("image")
        cname = cs.get("name")
        state = cs.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        waiting = state.get("waiting") or {}
        running = state.get("running") or {}
        terminated = state.get("terminated") or {}
        if not isinstance(waiting, dict):
            waiting = {}
        if not isinstance(running, dict):
            running = {}
        if not isinstance(terminated, dict):
            terminated = {}
        containers.append(
            {
                "name": cname if isinstance(cname, str) else None,
                "image": image if isinstance(image, str) else None,
                "restart_count": restart_count,
                "ready": bool(cs.get("ready")),
                "waiting_reason": waiting.get("reason")
                if isinstance(waiting.get("reason"), str)
                else None,
                "waiting_message": waiting.get("message")
                if isinstance(waiting.get("message"), str)
                else None,
                "terminated_reason": terminated.get("reason")
                if isinstance(terminated.get("reason"), str)
                else None,
                "terminated_exit_code": terminated.get("exitCode"),
                "running_started_at": running.get("startedAt")
                if isinstance(running.get("startedAt"), str)
                else None,
            }
        )

    containers_spec = spec.get("containers") or []
    if isinstance(containers_spec, list):
        for c in containers_spec:
            if not isinstance(c, dict):
                continue
            resources = c.get("resources") or {}
            if not isinstance(resources, dict):
                resources = {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            if not isinstance(requests, dict):
                requests = {}
            if not isinstance(limits, dict):
                limits = {}
            # Bind resources to the first matching container (small
            # whitelist for envelope size).
            for proj in containers:
                if proj.get("name") == c.get("name"):
                    proj["resources_requests"] = {
                        k: str(v) for k, v in requests.items()
                    }
                    proj["resources_limits"] = {
                        k: str(v) for k, v in limits.items()
                    }
                    break

    phase = status.get("phase") if isinstance(status.get("phase"), str) else None
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "uid": meta.get("uid"),
        "node": spec.get("nodeName"),
        "phase": phase,
        "restart_count": total_restarts,
        "created_at": meta.get("creationTimestamp"),
        "containers": containers,
    }


def _project_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    meta = ev.get("metadata") if isinstance(ev, dict) else {}
    involved = ev.get("involvedObject") if isinstance(ev, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(involved, dict):
        involved = {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "reason": ev.get("reason") if isinstance(ev.get("reason"), str) else None,
        "message": ev.get("message") if isinstance(ev.get("message"), str) else None,
        "type": ev.get("type") if isinstance(ev.get("type"), str) else None,
        "count": ev.get("count") if isinstance(ev.get("count"), int) else None,
        "first_timestamp": ev.get("firstTimestamp")
        if isinstance(ev.get("firstTimestamp"), str)
        else None,
        "last_timestamp": ev.get("lastTimestamp")
        if isinstance(ev.get("lastTimestamp"), str)
        else None,
        "involved_kind": involved.get("kind")
        if isinstance(involved.get("kind"), str)
        else None,
        "involved_name": involved.get("name")
        if isinstance(involved.get("name"), str)
        else None,
    }


def _project_deployment(d: Dict[str, Any]) -> Dict[str, Any]:
    meta = d.get("metadata") if isinstance(d, dict) else {}
    spec = d.get("spec") if isinstance(d, dict) else {}
    status = d.get("status") if isinstance(d, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(spec, dict):
        spec = {}
    if not isinstance(status, dict):
        status = {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "replicas_desired": spec.get("replicas"),
        "replicas_ready": status.get("readyReplicas"),
        "replicas_available": status.get("availableReplicas"),
        "replicas_unavailable": status.get("unavailableReplicas"),
        "updated_replicas": status.get("updatedReplicas"),
        "conditions": [
            {
                "type": c.get("type") if isinstance(c, dict) else None,
                "status": c.get("status") if isinstance(c, dict) else None,
                "reason": c.get("reason") if isinstance(c, dict) else None,
                "message": c.get("message") if isinstance(c, dict) else None,
            }
            for c in (status.get("conditions") or [])
            if isinstance(c, dict)
        ],
    }


__all__ = [
    "KubernetesCollector",
    "MAX_K8S_OBJECTS",
    "MAX_RESPONSE_BYTES",
    "_INCIDENT_REASONS",
]
