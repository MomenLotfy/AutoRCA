from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest

from pipeline import AnalysisPipeline, PipelineInput
from rca_request.rca_request_builder import (
    GenerationOptions,
    RCARequestBuilder,
    RCARequestBuilderError,
    RepositoryContext,
)

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
def builder(pipeline) -> RCARequestBuilder:
    # الوصول لمكوّنات AnalysisPipeline الداخلية عبر أسماء "خاصة" مقبول هنا
    # فقط لأننا داخل حزمة الاختبارات نفسها؛ الاستخدام الإنتاجي الطبيعي
    # سيمرّر RuleEngine/ScoringEngine/RulesConfig جاهزين من نفس النقطة
    # التي أنشأتهما (AnalysisPipeline).
    return RCARequestBuilder(
        rule_engine=pipeline._rule_engine,
        scoring_engine=pipeline._scoring_engine,
        taxonomy_index=pipeline._taxonomy_index,
        rules_config=pipeline._rules_config,
    )


def test_rca_request_builder_end_to_end_missing_env(pipeline, builder, rules_config):
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR20260716-100",
            sources={"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
        )
    )

    selected = result.selected
    assert selected is not None

    rca_request = builder.build(
        analysis_id=result.analysis_id,
        selected=selected,
        all_hypotheses=result.hypotheses,
        evidence_list=result.evidence_list,
        repository_context=RepositoryContext(
            full_name="momen/nexvault",
            branch="main",
            commit_sha="abc1234",
            environment="production",
        ),
        diff_source=MISSING_ENV_GIT_DIFF,
        log_source=MISSING_ENV_TRACEBACK,
    )

    payload = rca_request.to_dict()

    # التطابق الحرفي بين RCARequest.confidence و selected.score بعد التطبيع.
    assert payload["confidence"] == pytest.approx(selected.score, abs=1e-6)

    # resolved_severity يجب أن يعكس severity_policy الفعلي: FT001 + production
    # في rules.config.json يرفعها لـ "critical" (قاعدة SP001).
    assert payload["selected_hypothesis"]["resolved_severity"] == "critical"

    # دليلان مدعومان، صفر أدلة معارضة، صفر فرضيات مستبعدة (فرضية واحدة فقط
    # نشأت أصلًا في هذا السيناريو).
    assert len(payload["supporting_evidence"]) == 2
    assert payload["contradicting_evidence"] == []
    assert payload["excluded_hypotheses"] == []

    # كل evidence_id في supporting_evidence موجود فعليًا في evidence_list الأصلي.
    original_ids = {e["id"] for e in result.evidence_list}
    for summary in payload["supporting_evidence"]:
        assert summary["evidence_id"] in original_ids
        assert summary["summary"]  # لا يوجد ملخص فارغ

    # diff_excerpt وlog_excerpt غير فارغين ومقصوصان (truncated) بشكل صحيح.
    assert "PORT=8000" in payload["diff_excerpt"]
    assert payload["log_excerpt"] != ""

    assert payload["generation_options"] == GenerationOptions().to_dict()


def test_rca_request_builder_rejects_non_selected_hypothesis(pipeline, builder):
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260716-101", sources={"traceback": MISSING_ENV_TRACEBACK})
    )
    selected = result.selected
    assert selected is not None

    # نبني نسخة مزيفة بحالة غير selected للتحقق من الرفض الصريح.
    from dataclasses import replace

    not_selected = replace(selected, status="candidate")

    with pytest.raises(RCARequestBuilderError):
        builder.build(
            analysis_id=result.analysis_id,
            selected=not_selected,
            all_hypotheses=result.hypotheses,
            evidence_list=result.evidence_list,
            repository_context=RepositoryContext(
                full_name="momen/nexvault",
                branch="main",
                commit_sha="abc1234",
                environment="local",
            ),
        )


def test_rca_request_builder_local_environment_lowers_severity(pipeline, builder):
    """severity_policy.SP003: أي بيئة local تُخفَّض severity إلى low."""
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260716-102", sources={"traceback": MISSING_ENV_TRACEBACK})
    )
    selected = result.selected
    assert selected is not None

    rca_request = builder.build(
        analysis_id=result.analysis_id,
        selected=selected,
        all_hypotheses=result.hypotheses,
        evidence_list=result.evidence_list,
        repository_context=RepositoryContext(
            full_name="momen/nexvault",
            branch="dev",
            commit_sha="abc1234",
            environment="local",
        ),
    )

    assert rca_request.to_dict()["selected_hypothesis"]["resolved_severity"] == "low"
