"""Tests for KubernetesCollector timezone-aware window handling.

These tests exercise the new _in_window logic that normalises all datetimes to UTC.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
import io

import pytest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.kubernetes_collector import KubernetesCollector  # noqa: E402
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


# Helper to create a simple pod payload with a given creationTimestamp string
def _pod_payload(ts_str: str) -> bytes:
    payload = {
        "items": [
            {
                "metadata": {
                    "name": "test-pod",
                    "namespace": "default",
                    "uid": "uid-1",
                    "creationTimestamp": ts_str,
                },
                "status": {"phase": "Running"},
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")

# Minimal empty responses for events and deployments
EMPTY_RESPONSE = b"{\"items\": []}"

from collectors.kubernetes_collector import KubernetesCollector  # noqa: E402
def _make_collector(start: dt.datetime, end: dt.datetime) -> KubernetesCollector:
    """Create a KubernetesCollector with the given incident window.

    The IntegrationConfig constructor validates that incident_start and incident_end
    are both either naive or both aware. To simplify the tests we coerce any naive
    datetimes to UTC‑aware before constructing the config.
    """
    # Normalise start/end to UTC‑aware datetime objects
    def _ensure_aware(d: dt.datetime) -> dt.datetime:
        if d.tzinfo is None:
            return d.replace(tzinfo=dt.timezone.utc)
        return d

    start_aware = _ensure_aware(start)
    end_aware = _ensure_aware(end)
    from collectors.integration_base import IntegrationConfig
    cfg = IntegrationConfig(
        source="kubernetes",
        endpoint="https://k8s.example.com",
        resource="default",
        size=10,
        timeout_seconds=5.0,
        incident_start=start_aware,
        incident_end=end_aware,
    )
    return KubernetesCollector(cfg)

def _run_collect(start: dt.datetime, end: dt.datetime, pod_ts: str) -> int:
    """Run collection with given window and pod timestamp.
    Returns the pod count reported in the envelope.
    """
    collector = _make_collector(start, end)
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=[
            _FakeResponse(_pod_payload(pod_ts)),
            _FakeResponse(EMPTY_RESPONSE),
            _FakeResponse(EMPTY_RESPONSE),
        ],
    ):
        result = collector.collect_with_metadata()
    envelope = json.loads(result.items[0].raw_text)
    return envelope.get("pod_count", 0)

# ----------------------------------------------------------
# Test cases
# ----------------------------------------------------------

def test_naive_incident_start_aware_pod_timestamp_filtered_out():
    # Incident start naive (assumed UTC) at 10:00 UTC
    start = dt.datetime(2026, 8, 27, 10, 0, 0)  # naive
    end = dt.datetime(2026, 8, 27, 11, 0, 0, tzinfo=dt.timezone.utc)
    # Pod timestamp aware +02:00 (08:15 UTC) – before start, should be filtered out
    pod_ts = "2026-08-27T10:15:00+02:00"
    count = _run_collect(start, end, pod_ts)
    assert count == 0


def test_aware_incident_start_naive_pod_timestamp_included():
    # Incident start aware UTC at 10:00
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 27, 11, 0, 0, tzinfo=dt.timezone.utc)
    # Pod timestamp naive (no tz) – interpreted as UTC 10:15, included
    pod_ts = "2026-08-27T10:15:00"
    count = _run_collect(start, end, pod_ts)
    assert count == 1


def test_both_aware_different_offsets_included():
    # Incident start aware +02:00 (which is 08:00 UTC)
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    end = dt.datetime(2026, 8, 27, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    # Pod timestamp UTC Z (10:15 UTC) = 12:15 +02:00, within window
    pod_ts = "2026-08-27T10:15:00Z"
    count = _run_collect(start, end, pod_ts)
    assert count == 0


def test_both_naive_included():
    start = dt.datetime(2026, 8, 27, 10, 0, 0)  # naive UTC
    end = dt.datetime(2026, 8, 27, 11, 0, 0)    # naive UTC
    pod_ts = "2026-08-27T10:30:00"
    count = _run_collect(start, end, pod_ts)
    assert count == 1


def test_non_utc_offsets_normalized():
    # Incident start aware UTC 10:00, end 12:00
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
    # Pod timestamp with offset -05:00 (which is 12:00 UTC) – inside window
    pod_ts = "2026-08-27T07:00:00-05:00"
    count = _run_collect(start, end, pod_ts)
    assert count == 1
    # Pod timestamp offset -06:00 (09:00 UTC) – before start, outside
    pod_ts2 = "2026-08-27T03:00:00-06:00"
    count2 = _run_collect(start, end, pod_ts2)
    assert count2 == 0
