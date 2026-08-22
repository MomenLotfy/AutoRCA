"""
tests/test_final_rca_validator.py
-----------------------------------------------------------------------------
يبني RCARequest حقيقي من الـ pipeline، ثم يبني FinalRCA صالح يدويًا
(يحاكي استجابة LLM ملتزمة)، ويتحقق أن Validator يقبله. بعدها يعطّل كل
تحقق من التحققات الستة على حدة (عبر نسخة معدَّلة من FinalRCA الصالح)
ويتأكد أن Validator يرفضه بالسبب الصحيح تحديدًا.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Dict

import pytest

from pipeline import AnalysisPipeline, PipelineInput
from rca_request.rca_request_builder import RCARequestBuilder, RepositoryContext
from validation.final_rca_validator import FinalRCAValidator

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
def pipeline() -> AnalysisPipeline:
    return AnalysisPipeline.from_config_files(
        rules_config_path=RULES_CONFIG_PATH,
        taxonomy_path=TAXONOMY_PATH,
    )


@pytest.fixture(scope="module")
def builder(pipeline) -> RCARequestBuilder:
    return RCARequestBuilder(
        rule_engine=pipeline._rule_engine,
        scoring_engine=pipeline._scoring_engine,
        taxonomy_index=pipeline._taxonomy_index,
        rules_config=pipeline._rules_config,
    )


@pytest.fixture(scope="module")
def validator() -> FinalRCAValidator:
    return FinalRCAValidator()


@pytest.fixture()
def rca_request(pipeline, builder) -> Dict[str, object]:
    result = pipeline.run(
        PipelineInput(
            analysis_id="AR20260717-001",
            sources={"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
        )
    )
    request = builder.build(
        analysis_id=result.analysis_id,
        selected=result.selected,
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
    return request.to_dict()


@pytest.fixture()
def valid_final_rca(rca_request) -> Dict[str, object]:
    """
    يحاكي استجابة LLM ملتزمة بالكامل بقرار RCARequest — كل قيمة حساسة
    (id, confidence, severity) منسوخة حرفيًا من rca_request، وليست مُختلَقة.
    """
    evidence_ids = [e["evidence_id"] for e in rca_request["supporting_evidence"]]

    return {
        "schema_version": 1,
        "analysis_id": rca_request["analysis_id"],
        "selected_hypothesis_id": rca_request["selected_hypothesis"]["id"],
        "confidence": rca_request["confidence"],
        "explanation": {
            "summary": "Missing PORT environment variable caused the crash.",
            "reasoning": f"{evidence_ids[0]} shows a KeyError for PORT, confirmed by {evidence_ids[1]} in the diff.",
            "cited_evidence_ids": evidence_ids,
        },
        "fix": {
            "description": "Add PORT to the environment configuration.",
            "steps": ["Add PORT=8000 to .env", "Restart the service"],
            "cited_evidence_ids": [evidence_ids[0]],
        },
        "pull_request": {
            "title": "Fix: restore missing PORT environment variable",
            "branch_name": "fix/missing-port-env",
            "commit_message": "fix: restore PORT environment variable",
            "diff": "--- a/.env\n+++ b/.env\n@@ -1,2 +1,3 @@\n DEBUG=true\n+PORT=8000\n SECRET_KEY=xyz",
            "description": "Restores the PORT variable removed in a previous commit.",
        },
        "incident_report": {
            "title": "Deployment failure: missing PORT variable",
            "severity": rca_request["selected_hypothesis"]["resolved_severity"],
            "summary": "The service failed to start due to a missing PORT variable.",
            "root_cause": "PORT was removed from .env in a recent commit.",
            "impact": "Service was unavailable until the fix was applied.",
            "resolution": "Restored PORT=8000 in .env and redeployed.",
        },
        "generation_metadata": {
            "model": "test-llm-v1",
            "prompt_version": "1.0.0",
            "generated_at": "2026-07-17T10:00:00+00:00",
        },
    }


def test_valid_final_rca_passes(validator, rca_request, valid_final_rca):
    result = validator.validate(rca_request, valid_final_rca)
    assert result.is_valid, result.violations
    assert result.violations == []


def test_changed_hypothesis_id_is_rejected(validator, rca_request, valid_final_rca):
    tampered = copy.deepcopy(valid_final_rca)
    tampered["selected_hypothesis_id"] = "RC99"
    result = validator.validate(rca_request, tampered)
    assert not result.is_valid
    assert any("غيّر الفرضية" in v for v in result.violations)


def test_changed_confidence_is_rejected(validator, rca_request, valid_final_rca):
    tampered = copy.deepcopy(valid_final_rca)
    tampered["confidence"] = 0.99
    result = validator.validate(rca_request, tampered)
    assert not result.is_valid
    assert any("غيّر confidence" in v for v in result.violations)


def test_unknown_cited_evidence_id_is_rejected(validator, rca_request, valid_final_rca):
    tampered = copy.deepcopy(valid_final_rca)
    tampered["explanation"]["cited_evidence_ids"].append("E999")
    result = validator.validate(rca_request, tampered)
    assert not result.is_valid
    assert any("cited_evidence_ids" in v for v in result.violations)


def test_changed_severity_is_rejected(validator, rca_request, valid_final_rca):
    tampered = copy.deepcopy(valid_final_rca)
    tampered["incident_report"]["severity"] = "low"
    result = validator.validate(rca_request, tampered)
    assert not result.is_valid
    assert any("غيّر severity" in v for v in result.violations)


def test_pull_request_present_when_not_requested_is_rejected(validator, rca_request, valid_final_rca):
    rca_request_no_pr = copy.deepcopy(rca_request)
    rca_request_no_pr["generation_options"]["include_pr_diff"] = False

    result = validator.validate(rca_request_no_pr, valid_final_rca)
    assert not result.is_valid
    assert any("pull_request" in v for v in result.violations)


def test_incident_report_missing_when_requested_is_rejected(validator, rca_request, valid_final_rca):
    tampered = copy.deepcopy(valid_final_rca)
    tampered["incident_report"] = None
    result = validator.validate(rca_request, tampered)
    assert not result.is_valid
    assert any("incident_report" in v for v in result.violations)


def test_schema_violation_is_caught(validator, rca_request, valid_final_rca):
    tampered = copy.deepcopy(valid_final_rca)
    del tampered["fix"]  # حقل مطلوب في final_rca.schema.json
    result = validator.validate(rca_request, tampered)
    assert not result.is_valid
    assert any("مخالفة schema" in v for v in result.violations)


def test_validate_or_raise_raises_with_all_violations(validator, rca_request, valid_final_rca):
    from validation.final_rca_validator import FinalRCAValidationError

    tampered = copy.deepcopy(valid_final_rca)
    tampered["selected_hypothesis_id"] = "RC99"
    tampered["confidence"] = 0.01

    with pytest.raises(FinalRCAValidationError) as exc_info:
        validator.validate_or_raise(rca_request, tampered)

    assert len(exc_info.value.violations) == 2
