from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import pytest

from llm.client import LLMClientError
from llm.openai_client import OpenAICompatibleLLMClient
from llm.prompts import SYSTEM_PROMPT, build_messages
from llm.service import LLMAnalysisService
from pipeline import AnalysisPipeline, PipelineInput
from rca_request.rca_request_builder import (
    GenerationOptions,
    RCARequestBuilder,
    RepositoryContext,
)
from validation.final_rca_validator import FinalRCAValidator, FinalRCAValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MISSING_ENV_TRACEBACK = "KeyError: 'PORT'\n"
MISSING_ENV_GIT_DIFF = "diff --git a/.env b/.env\n-PORT=8000\n"


class FakeLLMClient:
    def __init__(self, response: Dict[str, Any]) -> None:
        self.response = response
        self.received = []

    def generate(self, request):
        self.received.append(request)
        return copy.deepcopy(self.response)


@pytest.fixture()
def request_and_expected():
    pipeline = AnalysisPipeline.from_config_files(
        PROJECT_ROOT / "rules/rules.config.json",
        PROJECT_ROOT / "taxonomy/taxonomy.yaml",
    )
    result = pipeline.run(PipelineInput(
        analysis_id="AR20260823-001",
        sources={"traceback": MISSING_ENV_TRACEBACK, "git_diff": MISSING_ENV_GIT_DIFF},
    ))
    builder = RCARequestBuilder(
        rule_engine=pipeline._rule_engine,
        scoring_engine=pipeline._scoring_engine,
        taxonomy_index=pipeline._taxonomy_index,
        rules_config=pipeline._rules_config,
    )
    request = builder.build(
        analysis_id=result.analysis_id,
        selected=result.selected,
        all_hypotheses=result.hypotheses,
        evidence_list=result.evidence_list,
        repository_context=RepositoryContext(
            full_name="org/repo", branch="main", commit_sha="abc", environment="production"
        ),
        diff_source=MISSING_ENV_GIT_DIFF,
        log_source=MISSING_ENV_TRACEBACK,
        generation_options=GenerationOptions(
            include_pr_diff=False,
            include_incident_report=False,
        ),
    )
    evidence_ids = [item["evidence_id"] for item in request.to_dict()["supporting_evidence"]]
    final_rca = {
        "schema_version": 1,
        "analysis_id": request.analysis_id,
        "selected_hypothesis_id": request.selected_hypothesis["id"],
        "confidence": request.confidence,
        "explanation": {
            "summary": "PORT is missing.",
            "reasoning": "The supplied evidence shows the missing variable.",
            "cited_evidence_ids": evidence_ids,
        },
        "fix": {
            "description": "Restore PORT.",
            "steps": ["Set PORT=8000", "Redeploy"],
            "cited_evidence_ids": evidence_ids[:1],
        },
        "pull_request": None,
        "incident_report": None,
        "generation_metadata": {
            "model": "fake-test-model",
            "prompt_version": "1.0.0",
            "generated_at": "2026-08-23T12:00:00+00:00",
        },
    }
    return request, final_rca


def test_llm_receives_only_structured_rca_request(request_and_expected):
    request, response = request_and_expected
    client = FakeLLMClient(response)
    result = LLMAnalysisService(client).generate_validated(request)
    assert result == response
    assert len(client.received) == 1
    assert client.received[0] is request
    assert isinstance(client.received[0].to_dict(), dict)


def test_prompt_contains_policy_and_only_request_payload(request_and_expected):
    request, _ = request_and_expected
    messages = build_messages(request)
    assert SYSTEM_PROMPT.startswith("You are the explanation layer of AutoRCA.")
    assert "MUST NOT determine a different root cause" in messages[0]["content"]
    assert messages[1]["content"] == __import__("json").dumps(
        request.to_dict(), ensure_ascii=False, separators=(",", ":")
    )


def test_validator_rejects_tampered_values_at_service_boundary(request_and_expected):
    request, response = request_and_expected
    tampered = copy.deepcopy(response)
    tampered["selected_hypothesis_id"] = "RC999"
    with pytest.raises(FinalRCAValidationError):
        LLMAnalysisService(FakeLLMClient(tampered)).generate_validated(request)


def test_fake_client_failure_is_explicit(request_and_expected):
    request, _ = request_and_expected

    class FailingClient:
        def generate(self, request):
            raise LLMClientError("provider unavailable")

    with pytest.raises(LLMClientError, match="provider unavailable"):
        LLMAnalysisService(FailingClient()).generate_validated(request)


def test_real_client_fails_fast_without_credentials(monkeypatch):
    monkeypatch.delenv("AUTORCA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("AUTORCA_LLM_MODEL", raising=False)
    with pytest.raises(LLMClientError, match="AUTORCA_LLM_API_KEY"):
        OpenAICompatibleLLMClient()