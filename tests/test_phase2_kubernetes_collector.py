"""Phase 2.3 — KubernetesCollector unit tests.

All HTTP boundaries are mocked at ``urllib.request.urlopen`` so no
real cluster is contacted. Mirrors the Phase 2.2 test patterns.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import socket
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.integration_base import (  # noqa: E402
    IntegrationConfig,
    IntegrationError,
)
from collectors.kubernetes_collector import (  # noqa: E402
    KubernetesCollector,
    MAX_K8S_OBJECTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cfg(
    *,
    resource: Optional[str] = "default",
    endpoint: str = "https://k8s.example.com",
    auth_env: Optional[str] = "AUTORCA_K8S_TOKEN",
    size: int = 50,
    timeout: float = 5.0,
    incident_start: Optional[str] = "2026-08-27T10:00:00+00:00",
    incident_end: Optional[str] = "2026-08-27T11:00:00+00:00",
    auth_scheme: str = "bearer",
) -> IntegrationConfig:
    def _iso(v):
        if v is None:
            return None
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))

    return IntegrationConfig(
        source="kubernetes",
        endpoint=endpoint,
        resource=resource,
        size=size,
        timeout_seconds=timeout,
        auth_env=auth_env,
        auth_scheme=auth_scheme,
        incident_start=_iso(incident_start),
        incident_end=_iso(incident_end),
    )


class _FakeResponse:
    """Minimal HTTP response with a finite body and a status attribute."""

    def __init__(self, body: bytes, status: int = 200):
        self._fp = io.BytesIO(body)
        self.status = status

    def read(self, _chunk: int = -1) -> bytes:
        return self._fp.read()

    def close(self):
        self._fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _urlopen_factory(responses: List[Any]) -> Callable:
    """Return a side_effect callable that pops responses in order.

    Each item may be:
      - bytes (treated as a successful 200 response with that body)
      - an Exception (raised)
      - a callable returning a response
    """
    state = {"calls": 0, "log": []}

    def _side_effect(request, timeout=None):
        state["calls"] += 1
        state["log"].append(
            {"full_url": getattr(request, "full_url", str(request))}
        )
        if not responses:
            raise RuntimeError(
                f"urlopen called more times than prepared (call "
                f"#{state['calls']}, url={getattr(request, 'full_url', '?')})"
            )
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        if isinstance(item, bytes):
            return _FakeResponse(item)
        return item

    _side_effect.log = state["log"]  # type: ignore[attr-defined]
    _side_effect.calls = lambda: state["calls"]  # type: ignore[attr-defined]
    return _side_effect


# ---------------------------------------------------------------------------
# Identity / availability
# ---------------------------------------------------------------------------
def test_name_is_kubernetes():
    assert KubernetesCollector.name == "kubernetes"


def test_is_available_true_with_namespace():
    collector = KubernetesCollector(_cfg(resource="default"))
    assert collector.is_available() is True


def test_is_available_true_with_all_namespaces():
    collector = KubernetesCollector(_cfg(resource="all-namespaces"))
    assert collector.is_available() is True


def test_is_available_false_when_resource_is_none():
    cfg = _cfg(resource=None)
    collector = KubernetesCollector(cfg)
    assert collector.is_available() is False


def test_is_available_false_when_endpoint_empty():
    cfg = _cfg()
    object.__setattr__(cfg, "endpoint", "")
    collector = KubernetesCollector(cfg)
    assert collector.is_available() is False


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------
def test_constructor_rejects_wrong_source():
    cfg = IntegrationConfig(source="elasticsearch", endpoint="http://x")
    with pytest.raises(IntegrationError):
        KubernetesCollector(cfg)


def test_constructor_rejects_invalid_namespace_grammar():
    cfg = IntegrationConfig(
        source="kubernetes",
        endpoint="http://x",
        resource="Invalid_Namespace",
    )
    with pytest.raises(IntegrationError):
        KubernetesCollector(cfg)


def test_constructor_rejects_basic_auth_scheme():
    cfg = IntegrationConfig(
        source="kubernetes",
        endpoint="http://x",
        resource="default",
        auth_env="AUTORCA_K8S_TOKEN",
        auth_scheme="basic",
    )
    with pytest.raises(IntegrationError):
        KubernetesCollector(cfg)


# ---------------------------------------------------------------------------
# Window validation
# ---------------------------------------------------------------------------
def test_window_exceeding_6h_rejected():
    with pytest.raises(ValueError):
        IntegrationConfig(
            source="kubernetes",
            endpoint="https://k8s",
            resource="default",
            incident_start=dt.datetime(2026, 8, 27, 10, 0, tzinfo=dt.timezone.utc),
            incident_end=dt.datetime(2026, 8, 27, 17, 0, tzinfo=dt.timezone.utc),
        )


def test_window_required_when_calling_collect():
    cfg = _cfg(incident_start=None, incident_end=None)
    collector = KubernetesCollector(cfg)
    with pytest.raises(IntegrationError):
        collector.collect()


# ---------------------------------------------------------------------------
# Bounded collection
# ---------------------------------------------------------------------------
def test_max_k8s_objects_constant_is_bounded():
    assert MAX_K8S_OBJECTS <= 500


# ---------------------------------------------------------------------------
# Successful collection
# ---------------------------------------------------------------------------
def test_collect_returns_envelope_with_pods_events_deployments(monkeypatch):
    pod_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "payment-api-abc",
                        "namespace": "default",
                        "uid": "u-1",
                        "creationTimestamp": "2026-08-27T10:15:00Z",
                    },
                    "spec": {"nodeName": "node-1"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {
                                "name": "app",
                                "image": "payment-api:1.0",
                                "restartCount": 3,
                                "ready": True,
                                "state": {
                                    "running": {
                                        "startedAt": "2026-08-27T10:20:00Z"
                                    }
                                },
                            }
                        ],
                    },
                }
            ]
        }
    ).encode("utf-8")
    event_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "evt-1",
                        "namespace": "default",
                    },
                    "reason": "OOMKilled",
                    "message": "Container exceeded memory limits",
                    "type": "Warning",
                    "count": 1,
                    "firstTimestamp": "2026-08-27T10:25:00Z",
                    "lastTimestamp": "2026-08-27T10:25:00Z",
                    "involvedObject": {
                        "kind": "Pod",
                        "name": "payment-api-abc",
                    },
                }
            ]
        }
    ).encode("utf-8")
    deployment_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "payment-api",
                        "namespace": "default",
                    },
                    "spec": {"replicas": 3},
                    "status": {
                        "readyReplicas": 2,
                        "availableReplicas": 2,
                        "unavailableReplicas": 1,
                    },
                }
            ]
        }
    ).encode("utf-8")

    side_effect = _urlopen_factory(
        [pod_payload, event_payload, deployment_payload]
    )
    monkeypatch.setenv("AUTORCA_K8S_TOKEN", "fakesecret-1234")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = KubernetesCollector(_cfg())
        result = collector.collect_with_metadata()
    assert len(result.items) == 1
    envelope = json.loads(result.items[0].raw_text)
    assert envelope["type"] == "kubernetes_state"
    assert envelope["pod_count"] == 1
    assert envelope["event_count"] == 1
    assert envelope["deployment_count"] == 1
    assert "OOMKilled" in envelope["incident_reasons"]
    assert result.metadata["endpoint"] == "https://k8s.example.com"
    assert result.metadata["namespace"] == "default"


def test_collect_with_all_namespaces_lists_first(monkeypatch):
    ns_payload = json.dumps(
        {"items": [{"metadata": {"name": "default"}}]}
    ).encode("utf-8")
    pod_payload = b'{"items":[]}'
    event_payload = b'{"items":[]}'
    deployment_payload = b'{"items":[]}'

    side_effect = _urlopen_factory(
        [ns_payload, pod_payload, event_payload, deployment_payload]
    )
    monkeypatch.setenv("AUTORCA_K8S_TOKEN", "fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = KubernetesCollector(_cfg(resource="all-namespaces"))
        result = collector.collect_with_metadata()
    assert "/api/v1/namespaces" in side_effect.log[0]["full_url"]
    assert result.metadata["all_namespaces"] is True
    envelope = json.loads(result.items[0].raw_text)
    assert "default" in envelope.get("namespaces_queried", [])


# ---------------------------------------------------------------------------
# Time window enforcement
# ---------------------------------------------------------------------------
def test_collect_filters_pods_outside_window(monkeypatch):
    pod_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "old",
                        "namespace": "default",
                        "creationTimestamp": "2020-01-01T00:00:00Z",
                    },
                    "status": {"phase": "Running"},
                },
                {
                    "metadata": {
                        "name": "new",
                        "namespace": "default",
                        "creationTimestamp": "2026-08-27T10:15:00Z",
                    },
                    "status": {"phase": "Running"},
                },
            ]
        }
    ).encode("utf-8")

    side_effect = _urlopen_factory(
        [pod_payload, b'{"items":[]}', b'{"items":[]}']
    )
    monkeypatch.setenv("AUTORCA_K8S_TOKEN", "fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = KubernetesCollector(_cfg())
        result = collector.collect_with_metadata()
    envelope = json.loads(result.items[0].raw_text)
    names = [p["name"] for p in envelope["pods"]]
    assert "new" in names
    assert "old" not in names


# ---------------------------------------------------------------------------
# Authentication / secret masking
# ---------------------------------------------------------------------------
def test_bearer_auth_header_applied(monkeypatch):
    captured: Dict[str, Any] = {}

    def _capture(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse(b'{"items":[]}')

    monkeypatch.setenv("AUTORCA_K8S_TOKEN", "mysecrettoken")
    with mock.patch("urllib.request.urlopen", side_effect=_capture):
        collector = KubernetesCollector(_cfg())
        collector._http_get_json(
            "https://k8s/api/v1/namespaces/default/pods",
            collector._build_headers("mysecrettoken"),
            5.0,
        )
    assert captured["auth"] == "Bearer mysecrettoken"


def test_authorization_scrubbed_from_error_message(monkeypatch):
    body = io.BytesIO(b'{"message":"authorization: Bearer leakedtoken"}')

    def _side_effect(req, timeout=None):
        raise urllib.error.HTTPError(
            url="http://x",
            code=401,
            msg="err",
            hdrs=None,
            fp=body,
        )

    monkeypatch.setenv("AUTORCA_K8S_TOKEN", "leakedtoken")
    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = KubernetesCollector(_cfg())
        with pytest.raises(IntegrationError) as ei:
            collector._http_get_json(
                "https://k8s/api/v1/namespaces/default/pods",
                collector._build_headers("leakedtoken"),
                5.0,
            )
    assert "leakedtoken" not in str(ei.value)
    assert "***" in str(ei.value)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_timeout_raises_integration_error(monkeypatch):
    def _side_effect(req, timeout=None):
        raise socket.timeout("k8s timed out")

    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = KubernetesCollector(_cfg())
        with pytest.raises(IntegrationError):
            collector._http_get_json(
                "https://k8s/api/v1/namespaces/default/pods",
                collector._build_headers(None),
                5.0,
            )


def test_http_500_raises_integration_error(monkeypatch):
    body = io.BytesIO(b'{"error":"server"}')

    def _side_effect(req, timeout=None):
        raise urllib.error.HTTPError(
            url="http://x",
            code=500,
            msg="err",
            hdrs=None,
            fp=body,
        )

    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = KubernetesCollector(_cfg())
        with pytest.raises(IntegrationError):
            collector._http_get_json(
                "https://k8s/api/v1/namespaces/default/pods",
                collector._build_headers(None),
                5.0,
            )


def test_connection_refused_raises_integration_error(monkeypatch):
    def _side_effect(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = KubernetesCollector(_cfg())
        with pytest.raises(IntegrationError):
            collector._http_get_json(
                "https://k8s/api/v1/namespaces/default/pods",
                collector._build_headers(None),
                5.0,
            )


def test_malformed_json_raises_integration_error(monkeypatch):
    def _side_effect(req, timeout=None):
        return _FakeResponse(b"NOT_JSON")

    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = KubernetesCollector(_cfg())
        with pytest.raises(IntegrationError):
            collector._http_get_json(
                "https://k8s/api/v1/namespaces/default/pods",
                collector._build_headers(None),
                5.0,
            )
