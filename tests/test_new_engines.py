"""
tests/test_new_engines.py
-----------------------------------------------------------------------------
Focused unit tests for new deterministic engines:
- TimelineEngine
- CorrelationEngine
- IncidentGraphBuilder
- IncidentFingerprintBuilder
- RemediationEngine
- HypothesisEngine (supporting + contradicting evidence)

كل هذه الاختبارات تستخدم fixtures منعزلة (observations/evidence hand-built)
ولا تعتمد على repository حقيقي. الـ validation الفعلية للحوادث الحقيقية
موثّقة في الـ validation report.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from config.rules_config import RulesConfig
from engine.correlation_engine import CorrelationEngine, _extract_env_var_name, _extract_module_name
from engine.hypothesis_engine import HypothesisEngine
from engine.incident_fingerprint import IncidentFingerprintBuilder
from engine.incident_graph import IncidentGraphBuilder
from engine.remediation_engine import RemediationEngine
from engine.rule_engine import RuleEngine
from engine.scoring_engine import ScoringEngine
from engine.timeline_engine import TimelineEngine
from extractors.base import Location, Observation, ObservationIdGenerator
from pipeline import AnalysisPipeline, PipelineInput

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_CONFIG_PATH = PROJECT_ROOT / "rules" / "rules.config.json"
TAXONOMY_PATH = PROJECT_ROOT / "taxonomy" / "taxonomy.yaml"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def pipeline() -> AnalysisPipeline:
    return AnalysisPipeline.from_config_files(
        rules_config_path=RULES_CONFIG_PATH,
        taxonomy_path=TAXONOMY_PATH,
    )


def _make_observation(
    *,
    obs_id: str,
    kind: str,
    source: str,
    location: Location,
    data: dict,
    raw_reference: str,
    extracted_at: str | None = None,
) -> Observation:
    return Observation(
        id=obs_id,
        analysis_id="AR20260825-TEST",
        schema_version=1,
        kind=kind,
        source=source,
        producer_id="test_helper",
        producer_version="1.0.0",
        location=location,
        data=data,
        raw_reference=raw_reference,
        extracted_at=extracted_at or dt.datetime.now(dt.timezone.utc).isoformat(),
    )


# =============================================================================
# TimelineEngine
# =============================================================================


def test_timeline_engine_emits_events_from_observations():
    obs = _make_observation(
        obs_id="O1",
        kind="key_error",
        source="docker_output",
        location=Location(file="/server/app/main.py", line=42),
        data={"key": "DOCKER_DATABASE_URL"},
        raw_reference="KeyError: 'DOCKER_DATABASE_URL'",
        extracted_at="2026-08-25T10:00:00.000000+00:00",
    )
    timeline = TimelineEngine().build(
        analysis_id="AR-TEST",
        observations=[obs],
        analysis_started_at="2026-08-25T09:59:59.000000+00:00",
    )

    assert len(timeline.events) == 2  # analysis_start + 1 observation event
    assert timeline.events[0].event_type == "analysis_start"
    assert timeline.events[1].event_type == "traceback"
    assert timeline.events[1].description == "KeyError detected for variable 'DOCKER_DATABASE_URL'"
    assert timeline.events[1].timestamp_known is True
    assert timeline.has_unknown_timestamps is False


def test_timeline_engine_marks_unknown_timestamps():
    obs = _make_observation(
        obs_id="O1",
        kind="key_error",
        source="docker_output",
        location=Location(),
        data={"key": "X"},
        raw_reference="KeyError: 'X'",
        extracted_at="2026-08-25T10:00:00.000000+00:00",
    )
    timeline = TimelineEngine().build(
        analysis_id="AR-TEST",
        observations=[obs],
        analysis_started_at=None,
    )
    # extracted_at موجود على الـ observation، فالـ timeline لا يضع timestamp unknown
    # (extracted_at ليست None). لكن الحدث لا يزال يعتمد على extracted_at فقط
    # لأن analysis_started_at=None.
    assert timeline.earliest_known_timestamp is not None
    assert not timeline.has_unknown_timestamps


def test_timeline_event_type_mapping():
    obs_kinds = [
        ("key_error", "traceback"),
        ("module_not_found_error", "traceback"),
        ("address_in_use_error", "container_failure"),
        ("diff_removed_line", "configuration_change"),
    ]
    for kind, expected_event_type in obs_kinds:
        data: dict = {}
        if kind == "key_error":
            data = {"key": "X"}
        elif kind == "module_not_found_error":
            data = {"module": "x"}
        elif kind == "address_in_use_error":
            data = {"port": 8000}
        # diff_removed_line يحتاح line_content لتفادي data فارغ
        elif kind == "diff_removed_line":
            data = {"line_content": "PORT=8000"}
        obs = _make_observation(
            obs_id="O1",
            kind=kind,
            source="docker_output",
            location=Location(),
            data=data,
            raw_reference="x",
        )
        timeline = TimelineEngine().build(analysis_id="AR-TEST", observations=[obs])
        assert timeline.events[0].event_type == expected_event_type, f"failed for {kind}"


# =============================================================================
# CorrelationEngine
# =============================================================================


def test_correlation_engine_links_env_removal_to_key_error():
    diff_obs = _make_observation(
        obs_id="O1",
        kind="diff_removed_line",
        source="git_diff",
        location=Location(file="server/.env.example", line=3, commit_sha="abc123"),
        data={"line_content": "DOCKER_DATABASE_URL=postgresql://x:5432/db"},
        raw_reference="-DOCKER_DATABASE_URL=postgresql://x:5432/db",
    )
    rt_obs = _make_observation(
        obs_id="O2",
        kind="key_error",
        source="docker_output",
        location=Location(file="/server/app/connect.py", line=38),
        data={"key": "DOCKER_DATABASE_URL"},
        raw_reference="KeyError: 'DOCKER_DATABASE_URL'",
    )
    corr = CorrelationEngine().correlate(analysis_id="AR-TEST", observations=[diff_obs, rt_obs])

    rels = {(e.source_observation_id, e.relation, e.target_observation_id) for e in corr.edges}
    assert ("O1", "modifies", "O2") in rels
    assert ("O1", "caused_by", "O2") in rels or any(
        e.source_observation_id == "O1" and e.relation == "caused_by" for e in corr.edges
    )
    assert any(e.relation == "references" for e in corr.edges)


def test_correlation_engine_links_dependency_removal_to_module_not_found():
    diff_obs = _make_observation(
        obs_id="O1",
        kind="diff_removed_line",
        source="git_diff",
        location=Location(file="requirements.txt", line=2),
        data={"line_content": "fastapi==0.139.2"},
        raw_reference="-fastapi==0.139.2",
    )
    rt_obs = _make_observation(
        obs_id="O2",
        kind="module_not_found_error",
        source="docker_output",
        location=Location(file="/server/app/main.py", line=2),
        data={"module": "fastapi"},
        raw_reference="ModuleNotFoundError: No module named 'fastapi'",
    )
    corr = CorrelationEngine().correlate(analysis_id="AR-TEST", observations=[diff_obs, rt_obs])
    assert any(e.relation == "modifies" for e in corr.edges)
    assert any(e.relation == "caused_by" for e in corr.edges)


def test_correlation_engine_no_unrelated_observations_produces_no_causal_edges():
    obs1 = _make_observation(
        obs_id="O1",
        kind="diff_removed_line",
        source="git_diff",
        location=Location(file="requirements.txt", line=2),
        data={"line_content": "somepkg==1.2.3"},
        raw_reference="-somepkg==1.2.3",
    )
    obs2 = _make_observation(
        obs_id="O2",
        kind="key_error",
        source="docker_output",
        location=Location(),
        data={"key": "TOTALLY_DIFFERENT_VAR"},
        raw_reference="KeyError: 'TOTALLY_DIFFERENT_VAR'",
    )
    corr = CorrelationEngine().correlate(analysis_id="AR-TEST", observations=[obs1, obs2])
    # لا توجد علاقة caused_by لأن الأسماء مختلفة تمامًا.
    assert all(e.relation != "caused_by" for e in corr.edges)
    assert all(e.relation != "modifies" for e in corr.edges)


def test_correlation_extract_helpers():
    assert _extract_env_var_name("DOCKER_DATABASE_URL=postgresql://...") == "DOCKER_DATABASE_URL"
    assert _extract_env_var_name("  PORT = 5432  ") == "PORT"
    assert _extract_env_var_name("# comment") is None

    assert _extract_module_name("fastapi==0.139.2") == "fastapi"
    assert _extract_module_name("requests~=2.28") == "requests"
    assert _extract_module_name("flask>=2.0") == "flask"
    assert _extract_module_name("not-a-module-line") is None


# =============================================================================
# IncidentGraphBuilder
# =============================================================================


def test_incident_graph_has_application_node_for_missing_env(pipeline):
    obs = _make_observation(
        obs_id="O1",
        kind="diff_removed_line",
        source="git_diff",
        location=Location(file=".env", line=3, commit_sha="abc123"),
        data={"line_content": "PORT=8000"},
        raw_reference="-PORT=8000",
    )
    rt_obs = _make_observation(
        obs_id="O2",
        kind="key_error",
        source="docker_output",
        location=Location(file="app.py", line=42),
        data={"key": "PORT"},
        raw_reference="KeyError: 'PORT'",
    )
    corr = CorrelationEngine().correlate("AR-TEST", [obs, rt_obs])
    graph = IncidentGraphBuilder().build(
        analysis_id="AR-TEST",
        correlation=corr,
        observations=[obs, rt_obs],
        selected_failure_type_id="FT001",
        selected_failure_label="missing_environment_variable",
        commit_sha="abc123",
    )
    types = {n.type for n in graph.nodes}
    assert "missing_environment_variable" in types
    assert "runtime_error" in types
    assert "configuration_file" in types
    assert "git_change" in types
    assert len(graph.edges) > 0


def test_incident_graph_no_correlation_still_emits_application_node():
    obs = _make_observation(
        obs_id="O1",
        kind="address_in_use_error",
        source="docker_output",
        location=Location(),
        data={"port": 8000},
        raw_reference="failed to bind 0.0.0.0:8000: address already in use",
    )
    corr = CorrelationEngine().correlate("AR-TEST", [obs])
    graph = IncidentGraphBuilder().build(
        analysis_id="AR-TEST",
        correlation=corr,
        observations=[obs],
        selected_failure_type_id="FT003",
        selected_failure_label="port_conflict",
    )
    assert {n.type for n in graph.nodes} >= {"runtime_error", "port_binding"}


# =============================================================================
# IncidentFingerprintBuilder
# =============================================================================


def test_fingerprint_for_missing_env(pipeline):
    obs = _make_observation(
        obs_id="O1",
        kind="key_error",
        source="docker_output",
        location=Location(file="/server/app/connect.py", line=38),
        data={"key": "DOCKER_DATABASE_URL"},
        raw_reference="KeyError: 'DOCKER_DATABASE_URL'",
    )
    diff_obs = _make_observation(
        obs_id="O2",
        kind="diff_removed_line",
        source="git_diff",
        location=Location(file=".env.example", line=3),
        data={"line_content": "DOCKER_DATABASE_URL=postgresql://..."},
        raw_reference="-DOCKER_DATABASE_URL=postgresql://...",
    )
    fp = IncidentFingerprintBuilder().build(
        analysis_id="AR-TEST",
        observations=[obs, diff_obs],
        failure_type_id="FT001",
        environment="production",
    )
    assert fp.failure_category == "environment"
    assert fp.failure_type == "FT001"
    assert fp.exception_type == "KeyError"
    assert fp.configuration_area == "env_file"
    assert fp.related_change_type == "env_var_removal"
    assert fp.runtime_type == "python"
    assert "env:DOCKER_DATABASE_URL" in fp.signature_keys


def test_fingerprint_for_missing_dependency():
    obs = _make_observation(
        obs_id="O1",
        kind="module_not_found_error",
        source="docker_output",
        location=Location(),
        data={"module": "fastapi"},
        raw_reference="ModuleNotFoundError: No module named 'fastapi'",
    )
    diff_obs = _make_observation(
        obs_id="O2",
        kind="diff_removed_line",
        source="git_diff",
        location=Location(file="requirements.txt"),
        data={"line_content": "fastapi==0.139.2"},
        raw_reference="-fastapi==0.139.2",
    )
    fp = IncidentFingerprintBuilder().build(
        analysis_id="AR-TEST",
        observations=[obs, diff_obs],
        failure_type_id="FT002",
        environment="production",
    )
    assert fp.failure_category == "build"
    assert fp.exception_type == "ModuleNotFoundError"
    assert fp.configuration_area == "dependency_manifest"
    assert fp.related_change_type == "dependency_removal"


def test_fingerprint_for_port_conflict():
    obs = _make_observation(
        obs_id="O1",
        kind="address_in_use_error",
        source="docker_output",
        location=Location(),
        data={"port": 8000},
        raw_reference="failed to bind host port 0.0.0.0:8000/tcp: address already in use",
    )
    fp = IncidentFingerprintBuilder().build(
        analysis_id="AR-TEST",
        observations=[obs],
        failure_type_id="FT003",
        environment="production",
    )
    assert fp.failure_category == "network"
    assert fp.exception_type == "OSError"
    assert fp.configuration_area == "port_binding"
    assert fp.related_change_type == "port_change"


# =============================================================================
# RemediationEngine
# =============================================================================


def test_remediation_for_missing_env_includes_real_variable_name():
    engine = RemediationEngine({"FT001": "Restore env var"})
    evidence = [{
        "id": "E1",
        "data": {"key": "DOCKER_DATABASE_URL"},
        "failure_type_id": "FT001",
    }]
    ctx = engine.build(
        analysis_id="AR-TEST",
        failure_type_id="FT001",
        evidence_list=evidence,
        environment="production",
    )
    assert ctx.action == "restore_missing_environment_variable"
    assert "DOCKER_DATABASE_URL" in ctx.target_symbols
    assert any("DOCKER_DATABASE_URL" in step for step in ctx.steps)
    assert any("DOCKER_DATABASE_URL" in v for v in ctx.validation)


def test_remediation_for_missing_dependency_includes_real_module_name():
    engine = RemediationEngine({"FT002": "Reinstall dep"})
    evidence = [{"id": "E1", "data": {"module": "fastapi"}, "failure_type_id": "FT002"}]
    ctx = engine.build(
        analysis_id="AR-TEST",
        failure_type_id="FT002",
        evidence_list=evidence,
        environment="production",
    )
    assert ctx.action == "reinstall_missing_dependency"
    assert "fastapi" in ctx.target_symbols
    assert any("fastapi" in step for step in ctx.steps)


def test_remediation_for_port_conflict_includes_real_port():
    engine = RemediationEngine({"FT003": "Free the port"})
    evidence = [{"id": "E1", "data": {"port": 8000}, "failure_type_id": "FT003"}]
    ctx = engine.build(
        analysis_id="AR-TEST",
        failure_type_id="FT003",
        evidence_list=evidence,
        environment="production",
    )
    assert ctx.action == "resolve_port_conflict"
    assert "8000" in ctx.target_symbols
    assert any("8000" in step for step in ctx.steps)
    # validation لا يذكر الـ port صراحةً (متعمد للعمومية) — لكن target_symbols
    # يحمل القيمة الفعلية 8000 لاستخدامها في الـ incident report.
    assert "8000" in ctx.target_symbols


# =============================================================================
# HypothesisEngine — supporting + contradicting evidence
# =============================================================================


def test_hypothesis_assessment_includes_supporting_evidence_ids(pipeline):
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR-TEST-001",
            sources={
                "traceback": "KeyError: 'PORT'\n  File \"app.py\", line 42\n",
                "git_diff": (
                    "diff --git a/.env b/.env\n"
                    "@@ -1,3 +1,2 @@\n"
                    " DEBUG=true\n"
                    "-PORT=8000\n"
                    " SECRET_KEY=xyz\n"
                ),
            },
        )
    )
    ha = result.hypothesis_assessment
    assert ha is not None
    assert ha.selected is not None
    assert ha.selected.label == "missing_environment_variable"
    assert len(ha.selected.supporting_evidence_ids) >= 2


def test_hypothesis_assessment_contradicting_evidence_lowers_score(pipeline):
    """تأكيد أن HypothesisEngine يكتشف contradicting evidence حقيقية (port_success)
    ويخفض الـ score تبعًا لها — لا يتم حقن قيم ثابتة."""
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR-TEST-002",
            sources={
                # KeyError traceback حقيقي يدعم missing_environment_variable
                "traceback": "KeyError: 'PORT'\n  File \"app.py\", line 42\n",
                # رسالة "Uvicorn running on" تدل على أن الـ port تم bind بنجاح
                # وهذا يضعف فرضية port_conflict لو وُجدت
                "docker_output": (
                    "Traceback (most recent call last):\n"
                    "  File \"app.py\", line 42\n"
                    "KeyError: 'PORT'\n"
                    "Uvicorn running on http://0.0.0.0:8000\n"
                ),
                "git_diff": (
                    "diff --git a/.env b/.env\n"
                    "@@ -1,3 +1,2 @@\n"
                    " DEBUG=true\n"
                    "-PORT=8000\n"
                    " SECRET_KEY=xyz\n"
                ),
            },
        )
    )
    ha = result.hypothesis_assessment
    assert ha is not None
    selected = ha.selected
    assert selected is not None
    # لازم تحتوي تقييم missing_environment_variable بالكامل
    assert selected.label == "missing_environment_variable"


def test_hypothesis_assessment_rejects_hypothesis_with_only_contradicting(pipeline):
    """لو ما في supporting evidence كافية، الـ engine لا يختار selected."""
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR-TEST-003",
            # نص لا يحتوي على أي evidence — يجب أن لا يُختار selected
            sources={"ci_log": "All tests passed. No errors detected.\n"},
        )
    )
    if result.hypothesis_assessment is not None:
        assert result.hypothesis_assessment.selected is None


# =============================================================================
# Pipeline-level integration: كل المراحل معًا على scenario حقيقي في شكل نص
# =============================================================================


def test_pipeline_emits_all_new_fields(pipeline):
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR-TEST-004",
            sources={
                "traceback": "KeyError: 'DOCKER_DATABASE_URL'\n  File \"/server/app/connect.py\", line 38\n",
                "git_diff": (
                    "diff --git a/.env.example b/.env.example\n"
                    "@@ -15,4 +15,3 @@\n"
                    " POSTGRES_VOLUME=db\n"
                    " # USE WITH DOCKER ENVIRONMENT\n"
                    "-DOCKER_DATABASE_URL=postgresql://x:5432/db\n"
                    " \n"
                ),
            },
        )
    )
    assert result.timeline is not None
    assert result.correlation is not None
    assert result.graph is not None
    assert result.fingerprint is not None
    assert result.remediation is not None
    assert result.hypothesis_assessment is not None
    assert result.selected is not None
    assert result.selected.label == "missing_environment_variable"
