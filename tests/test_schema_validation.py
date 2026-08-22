"""
tests/test_schema_validation.py
-----------------------------------------------------------------------------
يتحقق آليًا أن كل مخرج فعلي من الـ pipeline (Observation, Evidence,
Hypothesis, RCARequest) يطابق ملف الـ JSON Schema الخاص به حرفيًا، باستخدام
مكتبة jsonschema. هذا النوع من الاختبارات كان سيكشف مشكلة hypothesis.id
("RCmissing_env" بدل "RC1") مباشرة حتى بدون وجود أي اختبار وظيفي محدد لها،
لأنه يتحقق من العقد (Contract) نفسه وليس فقط من القيم المتوقعة.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import jsonschema
import pytest

from pipeline import AnalysisPipeline, PipelineInput
from rca_request.rca_request_builder import RCARequestBuilder, RepositoryContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_CONFIG_PATH = PROJECT_ROOT / "rules" / "rules.config.json"
TAXONOMY_PATH = PROJECT_ROOT / "taxonomy" / "taxonomy.yaml"
SCHEMAS_DIR = PROJECT_ROOT / "schemas"

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

PORT_CONFLICT_DOCKER_OUTPUT = """\
> app@1.0.0 start
> node server.js

Error: listen EADDRINUSE: address already in use :::8000
    at Server.setupListenHandle [as _listen2] (net.js:1330:16)
"""


def _load_schema(name: str) -> dict:
    with (SCHEMAS_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


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
    return RCARequestBuilder(
        rule_engine=pipeline._rule_engine,
        scoring_engine=pipeline._scoring_engine,
        taxonomy_index=pipeline._taxonomy_index,
        rules_config=pipeline._rules_config,
    )


@pytest.fixture(scope="module")
def observation_schema() -> dict:
    return _load_schema("observation.schema.json")


@pytest.fixture(scope="module")
def evidence_schema() -> dict:
    return _load_schema("evidence.schema.json")


@pytest.fixture(scope="module")
def hypothesis_schema() -> dict:
    return _load_schema("hypothesis.schema.json")


@pytest.fixture(scope="module")
def rca_request_schema() -> dict:
    return _load_schema("rca_request.schema.json")


# =============================================================================
# سيناريوهات end-to-end لكل الأنواع الثلاثة المطبَّقة، لضمان تغطية كل شكل
# بيانات (data payload) مختلف قد يظهر في كل schema.
# =============================================================================

SCENARIOS = {
    "missing_env": {"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
    "missing_dependency": {"traceback": MISSING_DEPENDENCY_TRACEBACK},
    "port_conflict": {"docker_output": PORT_CONFLICT_DOCKER_OUTPUT},
}


@pytest.mark.parametrize("scenario_name", list(SCENARIOS.keys()))
def test_observations_match_schema(pipeline, observation_schema, scenario_name):
    sources = SCENARIOS[scenario_name]
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260716-901", sources=sources)
    )
    assert result.observations, f"لا توجد observations للسيناريو {scenario_name}"
    for observation in result.observations:
        jsonschema.validate(instance=observation.to_dict(), schema=observation_schema)


@pytest.mark.parametrize("scenario_name", list(SCENARIOS.keys()))
def test_evidence_matches_schema(pipeline, evidence_schema, scenario_name):
    sources = SCENARIOS[scenario_name]
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260716-902", sources=sources)
    )
    assert result.evidence_list, f"لا توجد evidence للسيناريو {scenario_name}"
    for evidence in result.evidence_list:
        jsonschema.validate(instance=evidence, schema=evidence_schema)


@pytest.mark.parametrize("scenario_name", list(SCENARIOS.keys()))
def test_hypotheses_match_schema(pipeline, hypothesis_schema, scenario_name):
    sources = SCENARIOS[scenario_name]
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260716-903", sources=sources)
    )
    assert result.hypotheses, f"لا توجد hypotheses للسيناريو {scenario_name}"
    for hypothesis in result.hypotheses:
        jsonschema.validate(instance=hypothesis.to_dict(), schema=hypothesis_schema)


@pytest.mark.parametrize("scenario_name", list(SCENARIOS.keys()))
def test_rca_request_matches_schema(pipeline, builder, rca_request_schema, scenario_name):
    sources = SCENARIOS[scenario_name]
    result = pipeline.run(
        PipelineInput(analysis_id="AR20260716-904", sources=sources)
    )
    selected = result.selected
    assert selected is not None, f"لا توجد فرضية مختارة للسيناريو {scenario_name}"

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
        diff_source=sources.get("git_diff"),
        log_source=sources.get("traceback") or sources.get("docker_output"),
    )

    jsonschema.validate(instance=rca_request.to_dict(), schema=rca_request_schema)


def test_all_schema_files_are_themselves_valid_json_schema(
    observation_schema, evidence_schema, hypothesis_schema, rca_request_schema
):
    """تحقق إضافي: ملفات الـ schema نفسها صحيحة الصياغة كـ JSON Schema Draft 7."""
    for schema in (observation_schema, evidence_schema, hypothesis_schema, rca_request_schema):
        jsonschema.Draft7Validator.check_schema(schema)
