"""
tests/test_phase1_evidence_v2.py
-----------------------------------------------------------------------------
Phase 1.6 — Evidence Model V2 additive tests.

التأكد أن:
- Observation يقبل الحقول الجديدة الاختيارية
- to_dict يحذفها عندما تكون None
- EvidenceBuilder يُلحقها في الـ evidence dict فقط عند توفرها
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractors.base import Location, Observation
from extractors.registry import registry, VALID_SOURCES


def test_observation_accepts_v2_fields():
    obs = Observation(
        id="O1",
        analysis_id="AR20260827-001",
        schema_version=1,
        kind="container_metrics",
        source="docker_metrics",
        producer_id="docker_metrics_extractor",
        producer_version="1.0.0",
        location=Location(),
        data={"mem_percent": 95.0},
        raw_reference="mem_percent=95.0",
        extracted_at="2026-08-27T12:00:00Z",
        service="demo-api",
        resource="container:demo-api",
        normalized_value=95.0,
        timestamp_known=True,
    )
    assert obs.service == "demo-api"
    assert obs.resource == "container:demo-api"
    assert obs.normalized_value == 95.0
    assert obs.timestamp_known is True


def test_observation_v2_fields_default_none():
    obs = Observation(
        id="O2",
        analysis_id="AR20260827-001",
        schema_version=1,
        kind="key_error",
        source="traceback",
        producer_id="missing_env_traceback_extractor",
        producer_version="1.0.0",
        location=Location(),
        data={"key": "X"},
        raw_reference="KeyError: 'X'",
        extracted_at="2026-08-27T12:00:00Z",
    )
    assert obs.service is None
    assert obs.resource is None
    assert obs.normalized_value is None
    assert obs.timestamp_known is True  # default True


def test_observation_to_dict_omits_v2_when_none():
    obs = Observation(
        id="O3",
        analysis_id="AR20260827-001",
        schema_version=1,
        kind="key_error",
        source="traceback",
        producer_id="missing_env_traceback_extractor",
        producer_version="1.0.0",
        location=Location(),
        data={"key": "X"},
        raw_reference="KeyError: 'X'",
        extracted_at="2026-08-27T12:00:00Z",
    )
    d = obs.to_dict()
    assert "service" not in d
    assert "resource" not in d
    assert "normalized_value" not in d
    assert "timestamp_known" not in d  # default True is not emitted


def test_observation_to_dict_emits_timestamp_known_false():
    obs = Observation(
        id="O4",
        analysis_id="AR20260827-001",
        schema_version=1,
        kind="container_event",
        source="docker_events",
        producer_id="docker_event_extractor",
        producer_version="1.0.0",
        location=Location(),
        data={"event": "unknown"},
        raw_reference="{}",
        extracted_at="2026-08-27T12:00:00Z",
        timestamp_known=False,
    )
    d = obs.to_dict()
    assert d["timestamp_known"] is False


def test_valid_sources_includes_docker_sources():
    assert "docker_events" in VALID_SOURCES
    assert "docker_metrics" in VALID_SOURCES
    assert "host_metrics" in VALID_SOURCES


def test_evidence_builder_attaches_v2_fields():
    from pipeline import AnalysisPipeline
    from evidence.evidence_builder import EvidenceBuilder

    pipeline = AnalysisPipeline.from_config_files(
        "rules/rules.config.json", "taxonomy/taxonomy.yaml"
    )

    metrics_payload = json_module_dumps([
        {
            "type": "container_metrics",
            "container": "demo-api",
            "mem_percent": 95.0,
            "timestamp": "2026-08-27T12:00:00Z",
        }
    ])

    from pipeline import PipelineInput
    pi = PipelineInput(
        analysis_id="AR20260827-002",
        sources={"docker_metrics": metrics_payload},
        environment="production",
    )
    result = pipeline.run(pi)
    assert result.evidence_list
    e = result.evidence_list[0]
    # v2 fields attached because source Observation carries them.
    assert e.get("service") == "demo-api"
    assert e.get("resource") == "container:demo-api"
    assert e.get("normalized_value") == 95.0


def test_evidence_builder_omits_v2_for_phase0_payload():
    """Phase-0 key_error payload must NOT receive new V2 fields."""
    from pipeline import AnalysisPipeline, PipelineInput

    payload = "Traceback (most recent call last):\n  File \"x.py\"\nKeyError: 'DOCKER_DATABASE_URL'"
    pipeline = AnalysisPipeline.from_config_files(
        "rules/rules.config.json", "taxonomy/taxonomy.yaml"
    )
    pi = PipelineInput(
        analysis_id="AR20260827-003",
        sources={"traceback": payload},
        environment="production",
    )
    result = pipeline.run(pi)
    assert result.evidence_list
    e = result.evidence_list[0]
    # Phase-0 byte-shape preserved.
    assert "service" not in e
    assert "resource" not in e
    assert "normalized_value" not in e


def json_module_dumps(value):
    import json
    return json.dumps(value)