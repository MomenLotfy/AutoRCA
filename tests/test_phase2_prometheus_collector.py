"""Phase 2.2 — PrometheusCollector unit tests.

All tests mock the HTTP boundary (``urllib.request.urlopen``) so they
do not depend on a live Prometheus server. A single fake Prometheus
response helper covers the happy-path scenarios.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import socket
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.base import IncidentContext
from collectors.integration_base import (
    IntegrationConfig,
    IntegrationError,
)
from collectors.prometheus_collector import (
    MAX_RESPONSE_BYTES,
    MIN_STEP_SECONDS,
    PrometheusCollector,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    monkeypatch.setenv("AUTORCA_PROM_ALLOW_LOOPBACK", "1")


def _base_config(**overrides):
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 27, 11, 0, 0, tzinfo=dt.timezone.utc)
    kwargs = dict(
        source="prometheus",
        endpoint="http://127.0.0.1:9090",
        query="up",
        incident_start=start,
        incident_end=end,
        size=10,
        timeout_seconds=2.0,
        auth_env=None,
    )
    kwargs.update(overrides)
    return IntegrationConfig(**kwargs)


def _fake_prom_response(
    *,
    data=None,
    status=200,
    body_bytes=None,
):
    """Build a mock that mimics an ``http.client.HTTPResponse``.

    Returns a bare ``MagicMock`` that ``urllib.request.urlopen`` can
    return directly (the PrometheusCollector does not use the context
    manager protocol).
    """
    if body_bytes is None:
        if data is None:
            data = {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"__name__": "up", "job": "prometheus"},
                            "value": [1726500000.0, "1"],
                        },
                    ],
                },
            }
        body_bytes = json.dumps(data).encode("utf-8")

    response = mock.MagicMock()
    response.status = status
    # First read returns the body; subsequent reads return b"" (EOF).
    response.read.side_effect = [body_bytes, b""]
    response.close = mock.MagicMock()
    return response


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
def test_is_available_returns_false_when_no_query():
    cfg = _base_config(query=None)
    collector = PrometheusCollector(cfg)
    assert collector.is_available() is False


def test_is_available_returns_false_when_no_endpoint():
    cfg = _base_config()
    object.__setattr__(cfg, "endpoint", "")
    collector = PrometheusCollector(cfg)
    assert collector.is_available() is False


def test_is_available_returns_true_when_query_and_endpoint_set():
    collector = PrometheusCollector(_base_config())
    assert collector.is_available() is True


# ---------------------------------------------------------------------------
# Source check
# ---------------------------------------------------------------------------
def test_constructor_rejects_wrong_source():
    cfg = IntegrationConfig(
        source="elasticsearch",
        endpoint="http://127.0.0.1:9090",
        query="up",
    )
    with pytest.raises(IntegrationError):
        PrometheusCollector(cfg)


# ---------------------------------------------------------------------------
# Successful collection
# ---------------------------------------------------------------------------
def test_successful_collection_returns_one_collected_item():
    collector = PrometheusCollector(_base_config())
    ctx = _fake_prom_response()
    with mock.patch(
        "urllib.request.urlopen", return_value=ctx
    ) as urlopen:
        items = collector.collect()
    assert len(items) == 1
    payload = json.loads(items[0].raw_text)
    assert payload["type"] == "prometheus_query"
    assert payload["mode"] == "range"
    assert payload["query"] == "up"
    assert payload["series_count"] == 1
    assert urlopen.called


def test_collect_with_metadata_returns_summary():
    collector = PrometheusCollector(_base_config())
    ctx = _fake_prom_response()
    with mock.patch("urllib.request.urlopen", return_value=ctx):
        result = collector.collect_with_metadata()
    assert result.metadata["source"] == "prometheus"
    assert result.metadata["mode"] == "range"
    assert "auth_env" not in result.metadata
    assert len(result.items) == 1


# ---------------------------------------------------------------------------
# Query mode selection
# ---------------------------------------------------------------------------
def test_range_query_uses_query_range_endpoint():
    collector = PrometheusCollector(_base_config())
    ctx = _fake_prom_response()
    captured = {}
    real_urlopen = mock.MagicMock(return_value=ctx)

    def _capture(req, *args, **kwargs):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return ctx

    with mock.patch("urllib.request.urlopen", side_effect=_capture):
        collector.collect()
    assert "/api/v1/query_range" in captured["url"]
    assert "step=" in captured["url"]
    assert "query=up" in captured["url"]


def test_instant_query_used_when_only_one_bound():
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    cfg = _base_config(incident_end=None, incident_start=start)
    collector = PrometheusCollector(cfg)
    ctx = _fake_prom_response()
    captured = {}

    def _capture(req, *args, **kwargs):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return ctx

    with mock.patch("urllib.request.urlopen", side_effect=_capture):
        collector.collect()
    assert "/api/v1/query" in captured["url"]
    assert "/api/v1/query_range" not in captured["url"]
    assert "time=" in captured["url"]


def test_step_floor_is_30_seconds():
    # 30 minute window → step must still be ≥ 30s.
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(minutes=30)
    cfg = _base_config(incident_start=start, incident_end=end)
    collector = PrometheusCollector(cfg)
    ctx = _fake_prom_response()
    captured = {}

    def _capture(req, *args, **kwargs):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return ctx

    with mock.patch("urllib.request.urlopen", side_effect=_capture):
        collector.collect()
    # Extract step= from the URL.
    assert "step=" in captured["url"]
    import urllib.parse
    parsed = urllib.parse.urlparse(captured["url"])
    qs = urllib.parse.parse_qs(parsed.query)
    step = int(qs["step"][0])
    assert step >= MIN_STEP_SECONDS


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_no_window_raises_integration_error():
    cfg = IntegrationConfig(
        source="prometheus",
        endpoint="http://127.0.0.1:9090",
        query="up",
        incident_start=None,
        incident_end=None,
    )
    collector = PrometheusCollector(cfg)
    with pytest.raises(IntegrationError):
        collector.collect()


def test_no_query_raises_integration_error():
    cfg = IntegrationConfig(
        source="prometheus",
        endpoint="http://127.0.0.1:9090",
        incident_start=dt.datetime(2026, 8, 27, 10, tzinfo=dt.timezone.utc),
        incident_end=dt.datetime(2026, 8, 27, 11, tzinfo=dt.timezone.utc),
    )
    collector = PrometheusCollector(cfg)
    with pytest.raises(IntegrationError):
        collector.collect()


def test_http_timeout_raises_integration_error():
    collector = PrometheusCollector(_base_config())
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=socket.timeout("read timed out"),
    ):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_http_401_raises_integration_error():
    collector = PrometheusCollector(_base_config())
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:9090/api/v1/query_range",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"status":"error","error":"unauthorized"}'),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_http_500_raises_integration_error():
    collector = PrometheusCollector(_base_config())
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:9090/api/v1/query_range",
        code=500,
        msg="Server Error",
        hdrs=None,
        fp=io.BytesIO(b"oops"),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_malformed_json_raises_integration_error():
    collector = PrometheusCollector(_base_config())
    response = mock.MagicMock()
    response.status = 200
    response.read.side_effect = [b"<<not json>>", b""]
    response.close = mock.MagicMock()
    with mock.patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_oversized_response_raises_integration_error():
    collector = PrometheusCollector(_base_config())
    big = b"x" * (MAX_RESPONSE_BYTES + 1)
    response = _fake_prom_response(body_bytes=big)
    with mock.patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_connection_refused_raises_integration_error():
    collector = PrometheusCollector(_base_config())
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(
            ("ConnectionRefusedError", "refused")
        ),
    ):
        with pytest.raises(IntegrationError):
            collector.collect()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def test_bearer_auth_header_attached():
    cfg = _base_config(auth_env="AUTORCA_PROM_TOKEN", auth_scheme="bearer")
    collector = PrometheusCollector(cfg)
    with mock.patch.dict(os.environ, {"AUTORCA_PROM_TOKEN": "secret-token"}):
        ctx = _fake_prom_response()
        captured = {}

        def _capture(req, *args, **kwargs):
            captured["headers"] = dict(req.headers)
            return ctx

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            collector.collect()
        assert captured["headers"].get("Authorization") == "Bearer secret-token"


def test_secret_value_does_not_leak_into_items():
    cfg = _base_config(auth_env="AUTORCA_PROM_TOKEN", auth_scheme="bearer")
    collector = PrometheusCollector(cfg)
    with mock.patch.dict(os.environ, {"AUTORCA_PROM_TOKEN": "topsecret"}):
        ctx = _fake_prom_response()
        with mock.patch("urllib.request.urlopen", return_value=ctx):
            items = collector.collect()
    raw = items[0].raw_text
    assert "topsecret" not in raw
    assert items[0].metadata.get("auth_env") is None


def test_auth_env_name_scrubbed_from_metadata():
    cfg = _base_config(auth_env="AUTORCA_PROM_TOKEN")
    collector = PrometheusCollector(cfg)
    with mock.patch.dict(os.environ, {"AUTORCA_PROM_TOKEN": "x"}):
        ctx = _fake_prom_response()
        with mock.patch("urllib.request.urlopen", return_value=ctx):
            result = collector.collect_with_metadata()
    # The metadata block must never carry the env var name itself.
    assert "auth_env" not in result.metadata
    assert "AUTORCA_PROM_TOKEN" not in json.dumps(result.metadata)


# ---------------------------------------------------------------------------
# Sample projection
# ---------------------------------------------------------------------------
def test_matrix_response_projects_samples():
    data = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "up"},
                    "values": [
                        [1726500000.0, "1"],
                        [1726500030.0, "1"],
                    ],
                }
            ],
        },
    }
    collector = PrometheusCollector(_base_config())
    ctx = _fake_prom_response(data=data)
    with mock.patch("urllib.request.urlopen", return_value=ctx):
        items = collector.collect()
    payload = json.loads(items[0].raw_text)
    assert payload["series_count"] == 1
    assert payload["sample_count"] == 2
    assert payload["series"][0]["samples"][0][1] == 1.0


def test_invalid_metric_name_rejected():
    cfg = _base_config(index_pattern="bad metric!")
    collector = PrometheusCollector(cfg)
    ctx = _fake_prom_response()
    with mock.patch("urllib.request.urlopen", return_value=ctx):
        with pytest.raises(IntegrationError):
            collector.collect()


# ---------------------------------------------------------------------------
# Extractor round-trip
# ---------------------------------------------------------------------------
def test_extractor_produces_observations_of_generic_log_line():
    import os as _os
    sys.path.insert(0, str(PROJECT_ROOT))
    import extractors.prometheus_extractor as pe
    from extractors.base import ExtractionContext, ObservationIdGenerator

    envelope = {
        "type": "prometheus_query",
        "mode": "range",
        "query": "up",
        "metric_name": "up",
        "service": None,
        "resource": None,
        "incident_start": "2026-08-27T10:00:00+00:00",
        "incident_end": "2026-08-27T11:00:00+00:00",
        "step_seconds": 30,
        "size": 10,
        "status": 200,
        "sample_count": 1,
        "series_count": 1,
        "series": [
            {
                "labels": {"__name__": "up", "job": "prometheus"},
                "value": [1726500000.0, 1.0],
            }
        ],
    }
    ctx = ExtractionContext(
        analysis_id="AR20260827-100000",
        raw_content=json.dumps(envelope),
        id_generator=ObservationIdGenerator(),
    )
    obs_list = pe.PrometheusExtractor().extract(ctx)
    assert len(obs_list) == 1
    assert obs_list[0].kind == "generic_log_line"
    assert obs_list[0].source == "prometheus"
    assert obs_list[0].resource == "prometheus:up"


def test_extractor_handles_empty_envelope():
    from extractors.base import ExtractionContext, ObservationIdGenerator
    import extractors.prometheus_extractor as pe

    ctx = ExtractionContext(
        analysis_id="AR20260827-100000",
        raw_content="",
        id_generator=ObservationIdGenerator(),
    )
    assert pe.PrometheusExtractor().extract(ctx) == []


# ---------------------------------------------------------------------------
# Registry / pipeline integration
# ---------------------------------------------------------------------------
def test_registry_includes_prometheus():
    from extractors.registry import registry

    ids = {
        m.extractor_id
        for m in registry.all_metadata()
    }
    assert "prometheus_extractor" in ids


def test_pipeline_registers_prometheus_extractor():
    import extractors.prometheus_extractor  # noqa: F401

    from extractors.registry import registry

    cls_list = registry.get_extractor_classes_for_source("prometheus")
    assert any(cls.EXTRACTOR_ID == "prometheus_extractor" for cls in cls_list)


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------
def test_incident_context_fills_missing_window():
    ctx = IncidentContext(
        incident_start=dt.datetime(2026, 8, 27, 9, tzinfo=dt.timezone.utc),
        incident_end=dt.datetime(2026, 8, 27, 10, tzinfo=dt.timezone.utc),
    )
    collector = PrometheusCollector(_base_config(incident_start=None, incident_end=None))
    ctx_mock = _fake_prom_response()
    with mock.patch("urllib.request.urlopen", return_value=ctx_mock):
        items = collector.collect(ctx=ctx)
    assert items  # did not raise


def test_unbounded_window_rejected():
    cfg = _base_config(incident_start=None, incident_end=None)
    collector = PrometheusCollector(cfg)
    with pytest.raises(IntegrationError):
        collector.collect()
