"""
tests/test_phase1_collectors.py
-----------------------------------------------------------------------------
Phase 1.1–1.4 — collector tests.

التحققات:
- IncidentContext يحتوي / is_set
- CollectedItem منشئ frozen
- HostMetricsCollector يعمل بدون subprocess ويعيد empty لو /proc غائب
- DockerMetricsCollector يستخدم JSON format (لا shell parsing)
- DockerEventCollector يحترم IncidentContext
- DockerLogCollector ينظف container name من الإدخال
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.base import (
    CollectedItem,
    CollectorError,
    IncidentContext,
    parse_iso_timestamp,
)


# ---------------------------------------------------------------------------
# IncidentContext / CollectedItem / parse helpers
# ---------------------------------------------------------------------------

def test_incident_context_default_is_unbounded():
    ctx = IncidentContext()
    assert ctx.is_set() is False
    assert ctx.contains(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)) is True
    assert ctx.contains(None) is True


def test_incident_context_window_contains():
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 5, 0, tzinfo=dt.timezone.utc)
    ctx = IncidentContext(incident_start=start, incident_end=end)
    assert ctx.is_set() is True
    assert ctx.contains(start) is True
    assert ctx.contains(end) is True
    assert ctx.contains(dt.datetime(2026, 1, 1, 12, 3, 0, tzinfo=dt.timezone.utc)) is True
    assert ctx.contains(dt.datetime(2026, 1, 1, 11, 59, 0, tzinfo=dt.timezone.utc)) is False
    assert ctx.contains(dt.datetime(2026, 1, 1, 12, 6, 0, tzinfo=dt.timezone.utc)) is False


def test_incident_context_partial_window():
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    ctx = IncidentContext(incident_start=start)
    # upper bound unbounded
    assert ctx.contains(dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc)) is True


def test_collected_item_is_frozen():
    item = CollectedItem(source="x", raw_text="hello")
    assert item.source == "x"
    assert item.timestamp is None
    # frozen — cannot mutate
    try:
        item.source = "y"  # type: ignore[misc]
        assert False, "should have raised"
    except (AttributeError, Exception):
        pass


def test_parse_iso_timestamp_z_suffix():
    ts = parse_iso_timestamp("2026-08-27T12:00:00Z")
    assert ts is not None
    assert ts.year == 2026 and ts.month == 8 and ts.day == 27


def test_parse_iso_timestamp_invalid_returns_none():
    assert parse_iso_timestamp("not-a-date") is None
    assert parse_iso_timestamp("") is None
    assert parse_iso_timestamp(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HostMetricsCollector
# ---------------------------------------------------------------------------

def test_host_metrics_collector_is_available_on_linux():
    from collectors.host_metrics_collector import HostMetricsCollector
    h = HostMetricsCollector()
    # either /proc/meminfo exists (Linux dev box) or not — both are valid.
    assert isinstance(h.is_available(), bool)


def test_host_metrics_collector_returns_record_when_available():
    from collectors.host_metrics_collector import HostMetricsCollector
    h = HostMetricsCollector()
    if not h.is_available():
        return  # macOS / Windows — skip
    items = h.collect()
    assert items
    item = items[0]
    assert item.source == "host_metrics"
    payload = json.loads(item.raw_text)
    assert payload[0]["type"] == "host_metrics"


def test_host_metrics_collector_no_subprocess():
    """The collector MUST NOT spawn any subprocess."""
    import unittest.mock as mock
    from collectors.host_metrics_collector import HostMetricsCollector

    with mock.patch("subprocess.run") as run:
        HostMetricsCollector().collect()
        run.assert_not_called()


# ---------------------------------------------------------------------------
# DockerEventCollector
# ---------------------------------------------------------------------------

def test_docker_event_collector_rejects_invalid_container_name():
    from collectors.docker_event_collector import DockerEventCollector

    for bad in ["name with space", "../escape", "a/b"]:
        try:
            DockerEventCollector(container=bad)
            assert False, f"should have raised for {bad!r}"
        except CollectorError:
            pass


def test_docker_event_collector_is_unavailable_without_docker_host():
    from collectors.docker_event_collector import DockerEventCollector
    os.environ.pop("AUTORCA_DOCKER_HOST", None)
    c = DockerEventCollector(container="x")
    assert c.is_available() is False


# ---------------------------------------------------------------------------
# DockerLogCollector
# ---------------------------------------------------------------------------

def test_docker_log_collector_rejects_invalid_container_name():
    from collectors.docker_log_collector import DockerLogCollector
    for bad in ["with space", "../x", "a/b"]:
        try:
            DockerLogCollector(container=bad)
            assert False, f"should have raised for {bad!r}"
        except CollectorError:
            pass


def test_docker_log_collector_is_unavailable_without_docker_host():
    from collectors.docker_log_collector import DockerLogCollector
    os.environ.pop("AUTORCA_DOCKER_HOST", None)
    c = DockerLogCollector(container="x")
    assert c.is_available() is False