from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

from engine.rule_engine import Hypothesis
from engine.scoring_engine import ScoringEngine
from pipeline import AnalysisPipeline, PipelineInput

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_CONFIG_PATH = PROJECT_ROOT / "rules" / "rules.config.json"
TAXONOMY_PATH = PROJECT_ROOT / "taxonomy" / "taxonomy.yaml"


MISSING_ENV_TRACEBACK = """\
Traceback (most recent call last):
  File "app.py", line 42, in <module>
    port = os.environ['PORT']
KeyError: 'PORT'
"""

MISSING_ENV_GIT_DIFF = """\
diff --git a/.env b/.env
index abc1234..def5678 100644
--- a/.env
+++ b/.env
@@ -1,3 +1,2 @@
 DEBUG=true
-PORT=8000
 SECRET_KEY=xyz
"""

MISSING_DEPENDENCY_TRACEBACK = """\
Traceback (most recent call last):
  File "app.py", line 5, in <module>
    import requests
ModuleNotFoundError: No module named 'requests'
"""

MISSING_DEPENDENCY_GIT_DIFF = """\
diff --git a/requirements.txt b/requirements.txt
index 1111111..2222222 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,2 @@
 flask==2.0.1
-requests==2.28.0
 gunicorn==20.1.0
\\ No newline at end of file
"""

PORT_CONFLICT_DOCKER_OUTPUT = """\
> app@1.0.0 start
> node server.js

Error: listen EADDRINUSE: address already in use :::8000
    at Server.setupListenHandle [as _listen2] (net.js:1330:16)
    at listenInCluster (net.js:1378:12)
"""

UNRELATED_CI_LOG = """\
Running unit tests...
test_health_check ... ok
test_create_user ... ok
All 12 tests passed.
"""


@pytest.fixture(scope="module")
def rules_config() -> Dict[str, object]:
    with RULES_CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def pipeline() -> AnalysisPipeline:
    return AnalysisPipeline.from_config_files(
        rules_config_path=RULES_CONFIG_PATH,
        taxonomy_path=TAXONOMY_PATH,
    )


@pytest.fixture(scope="module")
def scoring_engine(pipeline) -> ScoringEngine:
    return pipeline._scoring_engine


def get_selected(hypotheses: List[Hypothesis]) -> Hypothesis | None:
    for h in hypotheses:
        if h.status == "selected":
            return h
    return None


def expected_score_for_single_classification_rule(rules_config: Dict[str, object], failure_type_id: str) -> float:
    hypothesis_rules = rules_config["hypothesis_rules"][failure_type_id]
    supporting_links = [l for l in hypothesis_rules["links"] if l["relation"] == "supports"]
    assert len(supporting_links) == 1
    return float(supporting_links[0]["weight"])


def expected_score_for_two_classification_rules(
    rules_config: Dict[str, object], failure_type_id: str, second_classification_rule_id: str
) -> float:
    base_weight = expected_score_for_single_classification_rule(rules_config, failure_type_id)
    corroboration_weight = float(
        rules_config["corroboration_rules"]["additional_evidence_weight"][second_classification_rule_id]
    )
    clamp = rules_config["decision_rules"]["score_clamp"]
    return max(clamp["min"], min(clamp["max"], base_weight + corroboration_weight))


# =============================================================================
# السيناريو 1: Missing Environment Variable (FT001)
# =============================================================================


def test_missing_env_scenario_end_to_end(pipeline, scoring_engine, rules_config):
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR20260715-001",
            sources={"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
        )
    )
    evidence_list, hypotheses = result.evidence_list, result.hypotheses

    assert len(evidence_list) == 2
    assert {e["failure_type_id"] for e in evidence_list} == {"FT001"}

    selected = get_selected(hypotheses)
    assert selected is not None
    assert selected.failure_type_id == "FT001"
    assert selected.label == "missing_environment_variable"

    expected_score = expected_score_for_two_classification_rules(
        rules_config, "FT001", second_classification_rule_id="CR002"
    )
    assert selected.score == pytest.approx(expected_score, abs=1e-6)

    confidence = scoring_engine.compute_confidence(selected.score)
    assert confidence == pytest.approx(expected_score, abs=1e-6)

    evidence_ids = {e["id"] for e in evidence_list}
    for link in selected.links:
        assert link.evidence_id in evidence_ids
        assert link.relation == "supports"


def test_missing_env_traceback_only_still_selected(pipeline, rules_config):
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260715-002", sources={"traceback": MISSING_ENV_TRACEBACK})
    )
    evidence_list, hypotheses = result.evidence_list, result.hypotheses

    assert len(evidence_list) == 1
    selected = get_selected(hypotheses)
    assert selected is not None

    expected_score = expected_score_for_single_classification_rule(rules_config, "FT001")
    assert selected.score == pytest.approx(expected_score, abs=1e-6)
    assert expected_score >= rules_config["decision_rules"]["min_score_to_select"]


# =============================================================================
# السيناريو 2: Missing Dependency (FT002)
# =============================================================================


def test_missing_dependency_scenario_end_to_end(pipeline, scoring_engine, rules_config):
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR20260715-003",
            sources={"traceback": MISSING_DEPENDENCY_TRACEBACK, "git_diff": MISSING_DEPENDENCY_GIT_DIFF},
        )
    )
    evidence_list, hypotheses = result.evidence_list, result.hypotheses

    assert len(evidence_list) == 2
    assert {e["failure_type_id"] for e in evidence_list} == {"FT002"}

    selected = get_selected(hypotheses)
    assert selected is not None
    assert selected.failure_type_id == "FT002"
    assert selected.label == "missing_dependency"

    expected_score = expected_score_for_two_classification_rules(
        rules_config, "FT002", second_classification_rule_id="CR004"
    )
    assert selected.score == pytest.approx(expected_score, abs=1e-6)

    confidence = scoring_engine.compute_confidence(selected.score)
    assert confidence == pytest.approx(expected_score, abs=1e-6)


def test_missing_dependency_no_newline_marker_does_not_shift_line_numbers(pipeline):
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260715-004", sources={"git_diff": MISSING_DEPENDENCY_GIT_DIFF})
    )
    evidence_list = result.evidence_list

    removed_line_evidence = [e for e in evidence_list if e["data"].get("line_content") == "requests==2.28.0"]
    assert len(removed_line_evidence) == 1


# =============================================================================
# السيناريو 3: Port Conflict (FT003)
# =============================================================================


def test_port_conflict_scenario_end_to_end(pipeline, scoring_engine, rules_config):
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260715-005", sources={"docker_output": PORT_CONFLICT_DOCKER_OUTPUT})
    )
    evidence_list, hypotheses = result.evidence_list, result.hypotheses

    assert len(evidence_list) == 1
    assert evidence_list[0]["failure_type_id"] == "FT003"
    assert evidence_list[0]["data"]["port"] == 8000

    selected = get_selected(hypotheses)
    assert selected is not None
    assert selected.failure_type_id == "FT003"
    assert selected.label == "port_conflict"

    expected_score = expected_score_for_single_classification_rule(rules_config, "FT003")
    assert selected.score == pytest.approx(expected_score, abs=1e-6)

    confidence = scoring_engine.compute_confidence(selected.score)
    assert confidence == pytest.approx(expected_score, abs=1e-6)


# =============================================================================
# سيناريو سلبي
# =============================================================================


def test_unrelated_log_produces_no_hypotheses(pipeline):
    result = pipeline.run(PipelineInput(analysis_id="AR20260715-006", sources={"ci_log": UNRELATED_CI_LOG}))
    assert result.evidence_list == []
    assert result.hypotheses == []


# =============================================================================
# اختبار Regression: corroboration لازم يعتمد على "أول دليل للفرضية"
# وليس "أول دليل لكل classification_rule_id" — bug ظهر فعليًا أول تشغيل
# حقيقي للـ pipeline وأنتج score=1.0 (بعد clamp) بدل 0.90 المتوقع، لأن كل
# classification_rule كانت بتاخد وزنها الكامل بدل ما يشترك اتنين منهم في
# نفس الفرضية بوزن أساسي واحد + وزن تعزيز واحد.
# =============================================================================


def test_corroboration_uses_first_evidence_per_hypothesis_not_per_rule(pipeline, rules_config):
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR20260716-REGRESSION-001",
            sources={"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
        )
    )
    selected = get_selected(result.hypotheses)
    assert selected is not None

    base_weight = expected_score_for_single_classification_rule(rules_config, "FT001")
    corroboration_weight = float(
        rules_config["corroboration_rules"]["additional_evidence_weight"]["CR002"]
    )
    # النتيجة يجب أن تساوي (وزن أساسي واحد + وزن تعزيز واحد)، وليس
    # (وزن أساسي × عدد الأدلة) — وهذا بالضبط ما كان يحدث في الـ bug.
    assert selected.score == pytest.approx(base_weight + corroboration_weight, abs=1e-6)
    assert selected.score < base_weight * 2  # يفشل هذا التحقق لو تكرر الـ bug


# =============================================================================
# اختبار Regression: hypothesis.id لازم يطابق pattern الـ schema (^RC[0-9]+$)
# bug ظهر فعليًا عند بناء أول RCARequest حقيقي: id طلع "RCmissing_env"
# بدل "RC1"، لأن الكود كان بيشتق الـ id من مفتاح hypotheses_catalog مباشرة
# بدل توليد رقم تسلسلي مستقل.
# =============================================================================

import re as _re

_HYPOTHESIS_ID_PATTERN = _re.compile(r"^RC[0-9]+$")


def test_hypothesis_id_matches_schema_pattern(pipeline):
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR20260716-REGRESSION-002",
            sources={"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
        )
    )
    assert result.hypotheses, "لا توجد فرضيات لاختبار شكل الـ id."
    for hypothesis in result.hypotheses:
        assert _HYPOTHESIS_ID_PATTERN.match(hypothesis.id), (
            f"hypothesis.id='{hypothesis.id}' لا يطابق pattern الـ schema ^RC[0-9]+$"
        )


def test_corroboration_score_is_order_independent(pipeline, rules_config):
    """
    Regression test لـ bug حقيقي اتكشف من التشغيل الفعلي عبر CLI: قبل
    التصحيح، additional_evidence_weight كانت غير متماثلة (CR002/CR004
    فقط)، فلو git_diff اتجمع قبل traceback، الـ CR001 (traceback) كانت
    تاخد وزن تعزيز = 0.0 بدل 0.35 لعدم وجودها في الجدول، فطلعت score=0.55
    بدل 0.90. الإصلاح: الجدول بقى متماثلًا (CR001 و CR002 نفس القيمة)،
    والنتيجة يجب أن تكون واحدة بصرف النظر عن ترتيب مفاتيح sources dict.
    """
    result_traceback_first = pipeline.run(
        PipelineInput(
            analysis_id="AR20260822-910",
            sources={"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
        )
    )
    result_diff_first = pipeline.run(
        PipelineInput(
            analysis_id="AR20260822-911",
            sources={"git_diff": MISSING_ENV_GIT_DIFF, "traceback": MISSING_ENV_TRACEBACK},
        )
    )

    selected_a = get_selected(result_traceback_first.hypotheses)
    selected_b = get_selected(result_diff_first.hypotheses)
    assert selected_a is not None and selected_b is not None
    assert selected_a.score == selected_b.score == pytest.approx(0.90, abs=1e-6)


# =============================================================================
# اتساق الـ Registry
# =============================================================================


def test_registry_has_all_three_mvp_extractors_registered(pipeline):
    from extractors.registry import registry

    registered_ids = {meta.extractor_id for meta in registry.all_metadata()}
    expected_mvp_extractor_ids = {
        "missing_env_traceback_extractor",
        "missing_dependency_traceback_extractor",
        "port_conflict_docker_extractor",
        "diff_line_extractor",
    }
    assert expected_mvp_extractor_ids.issubset(registered_ids)


def test_no_duplicate_extractor_ids_across_registered_metadata(pipeline):
    from extractors.registry import registry

    all_ids = [meta.extractor_id for meta in registry.all_metadata()]
    assert len(all_ids) == len(set(all_ids))
