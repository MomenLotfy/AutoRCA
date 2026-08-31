"""Phase 2.1 — ElasticsearchCollector unit tests.

All tests mock the HTTP boundary (``urllib.request.urlopen``) so they
do not depend on a live Elasticsearch cluster. A single fake ES response
helper covers the happy-path scenarios.
"""
from __future__ import annotations

import base64
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
from collectors.elasticsearch_collector import (
    MAX_RESPONSE_BYTES,
    ElasticsearchCollector,
)
from collectors.integration_base import (
    DEFAULT_TIMEOUT_SECONDS,
    IntegrationConfig,
    IntegrationError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    """Tests run against ``127.0.0.1``; enable the loopback override."""
    monkeypatch.setenv("AUTORCA_ES_ALLOW_LOOPBACK", "1")


def _base_config(**overrides):
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 27, 11, 0, 0, tzinfo=dt.timezone.utc)
    kwargs = dict(
        source="elasticsearch",
        endpoint="http://127.0.0.1:9200",
        index_pattern="logs-*",
        incident_start=start,
        incident_end=end,
        size=10,
        timeout_seconds=2.0,
    )
    kwargs.update(overrides)
    return IntegrationConfig(**kwargs)


def _fake_es_response(
    *,
    hits=None,
    status=200,
    body_bytes=None,
    chunk_size=64 * 1024,
):
    """Build a mock that mimics an ``http.client.HTTPResponse``."""
    if hits is None:
        hits = [
            {
                "_index": "logs-2026.08.27",
                "_id": "abc123",
                "_source": {
                    "@timestamp": "2026-08-27T10:30:00Z",
                    "message": "OutOfMemoryError: Java heap space",
                    "service": "payment-api",
                    "log.level": "ERROR",
                    "host.name": "pod-7",
                },
            }
        ]
    payload = {"hits": {"total": {"value": len(hits)}, "hits": hits}}
    encoded = json.dumps(payload).encode("utf-8")
    if body_bytes is not None:
        encoded = body_bytes

    response = mock.MagicMock()
    response.read.side_effect = [
        encoded[i:i + chunk_size] for i in range(0, len(encoded), chunk_size)
    ] + [b""]
    response.close = mock.MagicMock()
    return response


# ---------------------------------------------------------------------------
# 1. Successful collection
# ---------------------------------------------------------------------------
def test_successful_collection_returns_one_collected_item():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    response = _fake_es_response(
        hits=[
            {
                "_index": "logs-2026.08.27",
                "_id": "doc-1",
                "_source": {
                    "@timestamp": "2026-08-27T10:30:00Z",
                    "message": "NullPointerException",
                    "service": "billing",
                    "log.level": "ERROR",
                },
            },
            {
                "_index": "logs-2026.08.27",
                "_id": "doc-2",
                "_source": {
                    "@timestamp": "2026-08-27T10:45:00Z",
                    "message": "ConnectionRefused",
                    "service": "billing",
                    "log.level": "WARN",
                },
            },
        ]
    )
    with mock.patch(
        "urllib.request.urlopen", return_value=response
    ) as urlopen:
        items = collector.collect()

    assert len(items) == 1
    assert items[0].source == "elasticsearch"
    envelope = json.loads(items[0].raw_text)
    assert envelope["type"] == "elasticsearch_search"
    assert envelope["hit_count"] == 2
    assert len(envelope["hits"]) == 2
    assert envelope["hits"][0]["service"] == "billing"
    urlopen.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Time-window filter
# ---------------------------------------------------------------------------
def test_time_window_is_applied_to_query_body():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    response = _fake_es_response()
    with mock.patch(
        "urllib.request.urlopen", return_value=response
    ) as urlopen:
        collector.collect()

    call = urlopen.call_args
    request_obj = call.args[0]
    body = json.loads(request_obj.data.decode("utf-8"))
    range_clause = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
    assert range_clause["gte"].startswith("2026-08-27T10:00:00")
    assert range_clause["lte"].startswith("2026-08-27T11:00:00")


# ---------------------------------------------------------------------------
# 3. Service filter
# ---------------------------------------------------------------------------
def test_service_filter_is_emitted_when_configured():
    cfg = _base_config(service="payment-api")
    collector = ElasticsearchCollector(cfg)

    response = _fake_es_response()
    with mock.patch(
        "urllib.request.urlopen", return_value=response
    ) as urlopen:
        collector.collect()

    request_obj = urlopen.call_args.args[0]
    body = json.loads(request_obj.data.decode("utf-8"))
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"service": "payment-api"}} in filters


# ---------------------------------------------------------------------------
# 4. Bounded result count
# ---------------------------------------------------------------------------
def test_size_is_clamped_to_max():
    cfg = IntegrationConfig(
        **_base_config().__dict__ | {"size": 5000}
    )
    assert cfg.size == 1000


# ---------------------------------------------------------------------------
# 5. Timeout handling
# ---------------------------------------------------------------------------
def test_timeout_raises_integration_error():
    cfg = _base_config(timeout_seconds=0.5)
    collector = ElasticsearchCollector(cfg)

    with mock.patch(
        "urllib.request.urlopen",
        side_effect=socket.timeout("read timed out"),
    ):
        with pytest.raises(IntegrationError, match="timed out"):
            collector.collect()


# ---------------------------------------------------------------------------
# 6. HTTP error handling
# ---------------------------------------------------------------------------
def test_http_401_raises_integration_error():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    err = urllib.error.HTTPError(
        "http://127.0.0.1:9200/logs-*/_search", 401, "Unauthorized", {}, io.BytesIO(b"auth failed")
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError, match="HTTP 401"):
            collector.collect()


def test_http_500_raises_integration_error():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    err = urllib.error.HTTPError(
        "http://127.0.0.1:9200/logs-*/_search", 500, "Server Error", {}, io.BytesIO(b"boom")
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError, match="HTTP 500"):
            collector.collect()


# ---------------------------------------------------------------------------
# 7. Malformed response
# ---------------------------------------------------------------------------
def test_malformed_json_raises_integration_error():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    response = _fake_es_response(body_bytes=b"{not json")
    with mock.patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(IntegrationError, match="malformed JSON"):
            collector.collect()


# ---------------------------------------------------------------------------
# 8. Authentication handling
# ---------------------------------------------------------------------------
def test_basic_auth_header_is_sent_when_env_set(monkeypatch):
    monkeypatch.setenv("AUTORCA_ES_BASIC_AUTH", "user:secret-password")
    cfg = _base_config(auth_env="AUTORCA_ES_BASIC_AUTH")
    collector = ElasticsearchCollector(cfg)

    response = _fake_es_response()
    with mock.patch(
        "urllib.request.urlopen", return_value=response
    ) as urlopen:
        collector.collect()

    request_obj = urlopen.call_args.args[0]
    auth_header = request_obj.headers.get("Authorization")
    assert auth_header is not None
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
    assert decoded == "user:secret-password"


# ---------------------------------------------------------------------------
# 9. Secret masking
# ---------------------------------------------------------------------------
def test_secret_value_does_not_leak_into_items(monkeypatch):
    secret_value = "supersecret-token-abc123"
    monkeypatch.setenv("AUTORCA_ES_LEAK_TEST", secret_value)
    cfg = _base_config(auth_env="AUTORCA_ES_LEAK_TEST")
    collector = ElasticsearchCollector(cfg)

    response = _fake_es_response()
    with mock.patch(
        "urllib.request.urlopen", return_value=response
    ) as urlopen:
        items = collector.collect()

    raw = items[0].raw_text
    meta = items[0].metadata
    # Secret must never appear in raw_text or item metadata.
    assert secret_value not in raw
    assert secret_value not in str(meta)

    # Sanity: Authorization header was actually sent (basic auth base64).
    request_obj = urlopen.call_args.args[0]
    auth_header = request_obj.headers.get("Authorization")
    assert auth_header is not None
    decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
    assert decoded.endswith("supersecret-token-abc123")


def test_auth_env_name_scrubbed_from_integration_metadata(monkeypatch):
    secret_value = "another-secret-value"
    monkeypatch.setenv("AUTORCA_SCRUB_TEST", secret_value)
    cfg = _base_config(auth_env="AUTORCA_SCRUB_TEST")
    collector = ElasticsearchCollector(cfg)

    response = _fake_es_response()
    with mock.patch("urllib.request.urlopen", return_value=response):
        # Integration metadata must not contain the env var name.
        result = collector.collect_with_metadata()

    assert "AUTORCA_SCRUB_TEST" not in str(result.metadata)
    assert secret_value not in str(result.metadata)


# ---------------------------------------------------------------------------
# 10. Unsupported URL scheme
# ---------------------------------------------------------------------------
def test_unsupported_url_scheme_rejected():
    from api.security import validate_integration_url

    for bad in ("ftp://es.example.com", "javascript://x", "file:///etc/passwd"):
        with pytest.raises(ValueError, match="must use one of"):
            validate_integration_url("elasticsearch.url", bad)


def test_embedded_userinfo_rejected():
    from api.security import validate_integration_url

    with pytest.raises(ValueError, match="must not embed credentials"):
        validate_integration_url("elasticsearch.url", "https://user:pass@es.example.com")


# ---------------------------------------------------------------------------
# 11. Oversized response protection
# ---------------------------------------------------------------------------
def test_oversized_response_raises_integration_error():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    big_payload = b'{"hits":{"hits":[]}}' + b" " * (MAX_RESPONSE_BYTES + 1)

    response = mock.MagicMock()
    # Return one giant chunk so the cap is tripped.
    response.read.side_effect = [big_payload, b""]
    response.close = mock.MagicMock()

    with mock.patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(IntegrationError, match="exceeded"):
            collector.collect()


# ---------------------------------------------------------------------------
# 12. Elasticsearch unavailable
# ---------------------------------------------------------------------------
def test_connection_refused_raises_integration_error():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    err = urllib.error.URLError(socket.error(111, "Connection refused"))
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError, match="connection error"):
            collector.collect()


def test_dns_failure_raises_integration_error():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    err = urllib.error.URLError(socket.gaierror("Name or service not known"))
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError, match="connection error"):
            collector.collect()


# ---------------------------------------------------------------------------
# 13. Normalization into AutoRCA-compatible data
# ---------------------------------------------------------------------------
def test_extractor_produces_observations_of_generic_log_line():
    from extractors.base import (
        ExtractionContext,
        ObservationIdGenerator,
        VALID_OBSERVATION_KINDS,
    )
    from extractors.elasticsearch_extractor import ElasticsearchExtractor

    envelope = json.dumps({
        "type": "elasticsearch_search",
        "index_pattern": "logs-*",
        "service": "payment-api",
        "incident_start": "2026-08-27T10:00:00+00:00",
        "incident_end": "2026-08-27T11:00:00+00:00",
        "size": 10,
        "hit_count": 2,
        "status": 200,
        "hits": [
            {
                "index": "logs-2026.08.27",
                "id": "doc-1",
                "timestamp": "2026-08-27T10:30:00Z",
                "message": "OOMKilled",
                "service": "payment-api",
                "level": "ERROR",
                "host": "pod-7",
            },
            {
                "index": "logs-2026.08.27",
                "id": "doc-2",
                "timestamp": "2026-08-27T10:45:00Z",
                "message": "ConnectionRefused",
                "service": "payment-api",
                "level": "WARN",
                "host": "pod-7",
            },
        ],
    })

    ctx = ExtractionContext(
        analysis_id="AR20260827-001",
        raw_content=envelope,
        id_generator=ObservationIdGenerator(),
    )
    extractor = ElasticsearchExtractor()
    observations = extractor.extract(ctx)

    assert len(observations) == 2
    assert all(o.kind == "generic_log_line" for o in observations)
    assert "generic_log_line" in VALID_OBSERVATION_KINDS
    assert all(o.source == "elasticsearch" for o in observations)
    assert observations[0].service == "payment-api"
    assert observations[0].data["message"] == "OOMKilled"
    assert observations[0].data["es_index"] == "logs-2026.08.27"
    assert observations[0].data["es_doc_id"] == "doc-1"
    assert observations[0].timestamp_known is True


def test_extractor_handles_malformed_envelope():
    from extractors.base import ExtractionContext, ObservationIdGenerator
    from extractors.elasticsearch_extractor import ElasticsearchExtractor

    for bad in ("", "not-json", '{"hits": "not-a-list"}', '{"type": "other"}'):
        ctx = ExtractionContext(
            analysis_id="AR20260827-002",
            raw_content=bad,
            id_generator=ObservationIdGenerator(),
        )
        extractor = ElasticsearchExtractor()
        # No crash; either empty list or no hits parsed.
        assert isinstance(extractor.extract(ctx), list)


# ---------------------------------------------------------------------------
# 14. Empty result
# ---------------------------------------------------------------------------
def test_empty_es_response_does_not_crash():
    cfg = _base_config()
    collector = ElasticsearchCollector(cfg)

    payload = {"hits": {"total": {"value": 0}, "hits": []}}
    response = _fake_es_response(hits=[])

    with mock.patch("urllib.request.urlopen", return_value=response):
        items = collector.collect()

    assert len(items) == 1
    envelope = json.loads(items[0].raw_text)
    assert envelope["hit_count"] == 0
    assert envelope["hits"] == []


# ---------------------------------------------------------------------------
# 15. Phase 1 baseline preserved
# ---------------------------------------------------------------------------
def test_phase1_collector_still_importable():
    from collectors.docker_event_collector import DockerEventCollector
    from collectors.docker_log_collector import DockerLogCollector
    from collectors.docker_metrics_collector import DockerMetricsCollector
    from collectors.host_metrics_collector import HostMetricsCollector

    for cls in (
        DockerEventCollector,
        DockerLogCollector,
        DockerMetricsCollector,
        HostMetricsCollector,
    ):
        assert cls.__name__ == cls.__name__


def test_registry_includes_elasticsearch():
    from extractors.registry import VALID_SOURCES

    assert "elasticsearch" in VALID_SOURCES


def test_pipeline_registers_elasticsearch_extractor():
    from extractors.registry import registry

    metadata = registry.all_metadata()
    extractor_ids = [m.extractor_id for m in metadata]
    assert "elasticsearch_extractor" in extractor_ids
    es_meta = next(m for m in metadata if m.extractor_id == "elasticsearch_extractor")
    assert es_meta.source == "elasticsearch"
    assert es_meta.produces_kinds == ("generic_log_line",)
    assert es_meta.version == "1.0.0"


# ---------------------------------------------------------------------------
# Additional: validate_integration_url edge cases
# ---------------------------------------------------------------------------
def test_validate_integration_url_rejects_loopback_by_default(monkeypatch):
    from api.security import validate_integration_url

    monkeypatch.delenv("AUTORCA_ES_ALLOW_LOOPBACK", raising=False)
    with pytest.raises(ValueError, match="blocked network"):
        validate_integration_url("elasticsearch.url", "http://127.0.0.1:9200")


def test_validate_integration_url_accepts_loopback_when_allowed(monkeypatch):
    from api.security import validate_integration_url

    monkeypatch.setenv("AUTORCA_ES_ALLOW_LOOPBACK", "1")
    out = validate_integration_url(
        "elasticsearch.url", "http://127.0.0.1:9200"
    )
    assert out == "http://127.0.0.1:9200"


def test_validate_integration_url_rejects_missing_host():
    from api.security import validate_integration_url

    with pytest.raises(ValueError, match="missing a hostname"):
        validate_integration_url("elasticsearch.url", "http:///path")


# ---------------------------------------------------------------------------
# Additional: IncidentContext integration
# ---------------------------------------------------------------------------
def test_incident_context_fills_missing_window():
    """If incident_start is None on the config but the context provides it,
    the collector should use the context value."""
    cfg = IntegrationConfig(
        source="elasticsearch",
        endpoint="http://127.0.0.1:9200",
        index_pattern="logs-*",
        incident_start=None,
        incident_end=dt.datetime(2026, 8, 27, 11, 0, 0, tzinfo=dt.timezone.utc),
    )
    collector = ElasticsearchCollector(cfg)
    response = _fake_es_response()
    ctx = IncidentContext(
        incident_start=dt.datetime(2026, 8, 27, 9, 0, 0, tzinfo=dt.timezone.utc)
    )
    with mock.patch(
        "urllib.request.urlopen", return_value=response
    ) as urlopen:
        collector.collect(ctx)

    body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
    assert body["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"].startswith(
        "2026-08-27T09:00:00"
    )


def test_unbounded_window_rejected():
    """Collector must refuse to query without any time bound."""
    cfg = IntegrationConfig(
        source="elasticsearch",
        endpoint="http://127.0.0.1:9200",
        index_pattern="logs-*",
        incident_start=None,
        incident_end=None,
    )
    collector = ElasticsearchCollector(cfg)
    with pytest.raises(IntegrationError, match="time window"):
        collector.collect()
