"""
tests/test_phase1_classification_and_confidence.py
-----------------------------------------------------------------------------
Phase 1.7 + 1.8 — root cause / symptom role classification and explainable
confidence breakdown.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import AnalysisPipeline, PipelineInput


def _build_pipeline():
    return AnalysisPipeline.from_config_files(
        "rules/rules.config.json", "taxonomy/taxonomy.yaml"
    )


def _metrics_payload(container="demo-api", mem_percent=95.0, restart_count=0):
    return json.dumps([
        {
            "type": "container_metrics",
            "container": container,
            "mem_percent": mem_percent,
            "mem_usage_bytes": int(mem_percent * 1024 * 1024),
            "mem_limit_bytes": 100 * 1024 * 1024,
            "cpu_percent": 12.0,
            "pids": 5,
            "restart_count": restart_count,
            "state": "running",
            "timestamp": "2026-08-27T12:00:00Z",
        }
    ])


def _events_payload(events):
    return json.dumps(events)


def _host_payload(mem_percent=70.0):
    return json.dumps([
        {
            "type": "host_metrics",
            "mem_percent": mem_percent,
            "mem_total_bytes": 16_000_000_000,
            "mem_available_bytes": 4_000_000_000,
            "cpu_percent": 22.0,
            "load1": 1.5,
            "timestamp": "2026-08-27T12:00:00Z",
        }
    ])


# ---------------------------------------------------------------------------
# Phase 1.7 — role classification
# ---------------------------------------------------------------------------

def test_resource_exhaustion_selected():
    pipeline = _build_pipeline()
    pi = PipelineInput(
        analysis_id="AR20260827-100",
        sources={
            "docker_metrics": _metrics_payload(),
            "docker_events": _events_payload([
                {"type": "container_event", "event": "oom", "container": "demo-api",
                 "timestamp": "2026-08-27T12:00:01Z", "actor": {"name": "demo-api"}},
            ]),
        },
        environment="production",
    )
    result = pipeline.run(pi)
    assert result.selected_failure_type_id() == "FT011"


def test_resource_exhaustion_evidence_roles():
    pipeline = _build_pipeline()
    pi = PipelineInput(
        analysis_id="AR20260827-101",
        sources={
            "docker_metrics": _metrics_payload(),
            "docker_events": _events_payload([
                {"type": "container_event", "event": "oom", "container": "demo-api",
                 "timestamp": "2026-08-27T12:00:01Z", "actor": {"name": "demo-api"}},
                {"type": "container_event", "event": "die", "container": "demo-api",
                 "timestamp": "2026-08-27T12:00:02Z", "actor": {"name": "demo-api"}},
            ]),
            "host_metrics": _host_payload(mem_percent=96.0),  # ≥95 → CR012 fires
        },
        environment="production",
    )
    result = pipeline.run(pi)
    selected = result.hypothesis_assessment.selected
    assert selected is not None
    roles = selected.evidence_roles
    # Mem-metric and oom event → root_cause. die event → symptom. host → contributing.
    assert "root_cause" in roles.values()
    assert "symptom" in roles.values()
    assert "contributing_factor" in roles.values()


def test_resource_exhaustion_severity_escalated_in_production():
    pipeline = _build_pipeline()
    pi = PipelineInput(
        analysis_id="AR20260827-102",
        sources={
            "docker_metrics": _metrics_payload(),
            "docker_events": _events_payload([
                {"type": "container_event", "event": "kill", "container": "demo-api",
                 "timestamp": "2026-08-27T12:00:01Z", "actor": {"name": "demo-api"}},
            ]),
        },
        environment="production",
    )
    result = pipeline.run(pi)
    selected = result.hypothesis_assessment.selected
    assert selected is not None
    assert selected.severity == "critical"


# ---------------------------------------------------------------------------
# Phase 1.8 — explainable confidence breakdown
# ---------------------------------------------------------------------------

def test_explainable_confidence_breakdown_present():
    pipeline = _build_pipeline()
    pi = PipelineInput(
        analysis_id="AR20260827-103",
        sources={
            "docker_metrics": _metrics_payload(),
            "docker_events": _events_payload([
                {"type": "container_event", "event": "oom", "container": "demo-api",
                 "timestamp": "2026-08-27T12:00:01Z", "actor": {"name": "demo-api"}},
            ]),
            "docker_output": (
                "2026-08-27T12:00:00.500Z ERR Container exited with non-zero "
                "exit code 137 (OOMKilled)"
            ),
        },
        environment="production",
    )
    result = pipeline.run(pi)
    selected = result.hypothesis_assessment.selected
    assert selected is not None
    breakdown = selected.confidence_breakdown
    assert "base" in breakdown
    assert "matching_evidence_bonus" in breakdown
    assert "temporal_correlation_bonus" in breakdown
    assert "resource_correlation_bonus" in breakdown
    assert "contradiction_penalty" in breakdown
    assert "final_score" in breakdown
    # Boosts nonzero for this rich evidence set.
    assert breakdown["final_score"] >= breakdown["base"] - 1e-6


def test_explainable_confidence_phase0_no_breakdown_for_ft001():
    """Phase-0 key_error → FT001 must produce the same confidence as
    Phase 0: only base, no bonuses, no penalty. The breakdown is still
    emitted but should equal the Phase-0 value."""
    from engine.scoring_engine import ScoringEngine

    scoring = ScoringEngine(_build_pipeline()._rules_config)  # noqa: SLF001
    breakdown = scoring.compute_explainable_confidence(
        raw_score=0.55,
        matching_evidence_count=1,
        has_temporal_correlation=False,
        has_resource_correlation=False,
        contradiction_count=0,
    )
    assert breakdown.base == 0.55
    assert breakdown.matching_evidence_bonus == 0.0  # only 1 supporting evidence
    assert breakdown.temporal_correlation_bonus == 0.0
    assert breakdown.resource_correlation_bonus == 0.0
    assert breakdown.contradiction_penalty == 0.0
    assert breakdown.final_score == 0.55


def test_explainable_confidence_contradiction_lowers_score():
    from engine.scoring_engine import ScoringEngine

    scoring = ScoringEngine(_build_pipeline()._rules_config)  # noqa: SLF001
    breakdown = scoring.compute_explainable_confidence(
        raw_score=0.55,
        matching_evidence_count=1,
        contradiction_count=1,
    )
    assert breakdown.contradiction_penalty == 0.10
    assert breakdown.final_score < breakdown.base


def test_explainable_confidence_matching_bonus_capped():
    from engine.scoring_engine import ScoringEngine

    scoring = ScoringEngine(_build_pipeline()._rules_config)  # noqa: SLF001
    breakdown = scoring.compute_explainable_confidence(
        raw_score=0.50,
        matching_evidence_count=10,
        has_temporal_correlation=True,
        has_resource_correlation=True,
        contradiction_count=0,
    )
    # max_bonuses_total = 0.20
    assert breakdown.bonuses_total_before_clamp <= 0.20
    assert breakdown.final_score <= 1.0


# ---------------------------------------------------------------------------
# IncidentContext time window filter
# ---------------------------------------------------------------------------

def test_pipeline_time_window_filter_drops_out_of_window_observations():
    """When the window is fully bounded, observations outside it MUST be
    dropped. We use a Phase-0 traceback for simplicity and check that
    an out-of-window extracted_at is filtered out."""
    pipeline = _build_pipeline()
    # Force extracted_at via docker_output payload (no timestamp tricks —
    # we craft the observation through extraction). The window test uses
    # PipelineInput.incident_start/end with a wide enough window that
    # today's date is included. Then a narrow window that excludes today.
    wide = PipelineInput(
        analysis_id="AR20260827-110",
        sources={
            "traceback": "Traceback (most recent call last):\nKeyError: 'X'"
        },
        environment="production",
    )
    result_wide = pipeline.run(wide)
    assert result_wide.evidence_list

    narrow = PipelineInput(
        analysis_id="AR20260827-110",
        sources={
            "traceback": "Traceback (most recent call last):\nKeyError: 'X'"
        },
        environment="production",
        incident_start=__import__("datetime").datetime(2000, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        incident_end=__import__("datetime").datetime(2000, 1, 2, tzinfo=__import__("datetime").timezone.utc),
    )
    result_narrow = pipeline.run(narrow)
    assert not result_narrow.evidence_list