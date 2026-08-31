"""Phase 2.3 — CI/CD collector unit tests (GitHub Actions, GitLab CI, Jenkins).

All HTTP boundaries are mocked at ``urllib.request.urlopen`` so no
real CI provider is contacted. Mirrors the Phase 2.2 test patterns.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import socket
import sys
import urllib.error
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.cicd_provider_base import (  # noqa: E402
    CICDProvider,
    MAX_PIPELINES_PER_WINDOW,
    MAX_STAGES_PER_PIPELINE,
    PipelineRun,
)
from collectors.github_actions_collector import (  # noqa: E402
    GitHubActionsCollector,
)
from collectors.gitlab_ci_collector import (  # noqa: E402
    GitLabCICollector,
)
from collectors.integration_base import (  # noqa: E402
    IntegrationConfig,
    IntegrationError,
)
from collectors.jenkins_collector import (  # noqa: E402
    JenkinsCollector,
)


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------
def _iso(v: Optional[str]) -> Optional[dt.datetime]:
    if v is None:
        return None
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))


def _cfg(
    *,
    source: str,
    endpoint: str = "https://example.com",
    resource: Optional[str] = "octocat/Hello-World",
    auth_env: Optional[str] = "AUTORCA_TOKEN",
    auth_scheme: str = "bearer",
    incident_start: Optional[str] = "2026-08-27T10:00:00+00:00",
    incident_end: Optional[str] = "2026-08-27T11:00:00+00:00",
) -> IntegrationConfig:
    return IntegrationConfig(
        source=source,
        endpoint=endpoint,
        resource=resource,
        auth_env=auth_env,
        auth_scheme=auth_scheme,
        incident_start=_iso(incident_start),
        incident_end=_iso(incident_end),
    )


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


def _urlopen_factory(responses: List[Any]) -> Callable:
    """Return a side_effect callable that pops responses in order."""
    state = {"calls": 0, "log": []}

    def _side_effect(request, timeout=None):
        state["calls"] += 1
        state["log"].append(
            {"full_url": getattr(request, "full_url", str(request))}
        )
        if not responses:
            raise RuntimeError(
                f"urlopen called more times than prepared "
                f"(call #{state['calls']})"
            )
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        if isinstance(item, bytes):
            return _FakeResponse(item)
        return item

    _side_effect.log = state["log"]  # type: ignore[attr-defined]
    return _side_effect


# ---------------------------------------------------------------------------
# Generic / provider-protocol assertions
# ---------------------------------------------------------------------------
def test_max_pipelines_constant_bounded():
    assert MAX_PIPELINES_PER_WINDOW <= 200


def test_max_stages_per_pipeline_constant_bounded():
    assert MAX_STAGES_PER_PIPELINE <= 100


def test_github_implements_cicd_provider_protocol():
    c = GitHubActionsCollector(_cfg(source="github_actions"))
    assert isinstance(c, CICDProvider)


def test_gitlab_implements_cicd_provider_protocol():
    c = GitLabCICollector(_cfg(source="gitlab_ci"))
    assert isinstance(c, CICDProvider)


def test_jenkins_implements_cicd_provider_protocol():
    c = JenkinsCollector(_cfg(source="jenkins"))
    assert isinstance(c, CICDProvider)


# ---------------------------------------------------------------------------
# Constructor / availability — GitHub Actions
# ---------------------------------------------------------------------------
def test_github_constructor_rejects_wrong_source():
    cfg = _cfg(source="gitlab_ci")
    with pytest.raises(IntegrationError):
        GitHubActionsCollector(cfg)


def test_github_constructor_rejects_missing_resource():
    cfg = _cfg(source="github_actions", resource=None)
    with pytest.raises(IntegrationError):
        GitHubActionsCollector(cfg)


def test_github_is_available_false_without_resource():
    cfg = _cfg(source="github_actions")
    object.__setattr__(cfg, "resource", None)
    c = GitHubActionsCollector.__new__(GitHubActionsCollector)
    c._config = cfg
    assert c.is_available() is False


def test_github_is_available_true_when_configured():
    c = GitHubActionsCollector(_cfg(source="github_actions"))
    assert c.is_available() is True


# ---------------------------------------------------------------------------
# Constructor / availability — GitLab CI
# ---------------------------------------------------------------------------
def test_gitlab_constructor_rejects_wrong_source():
    with pytest.raises(IntegrationError):
        GitLabCICollector(_cfg(source="jenkins"))


def test_gitlab_constructor_rejects_missing_resource():
    with pytest.raises(IntegrationError):
        GitLabCICollector(_cfg(source="gitlab_ci", resource=None))


def test_gitlab_is_available_true_when_configured():
    c = GitLabCICollector(_cfg(source="gitlab_ci"))
    assert c.is_available() is True


# ---------------------------------------------------------------------------
# Constructor / availability — Jenkins
# ---------------------------------------------------------------------------
def test_jenkins_constructor_rejects_wrong_source():
    with pytest.raises(IntegrationError):
        JenkinsCollector(_cfg(source="github_actions"))


def test_jenkins_constructor_rejects_missing_resource():
    with pytest.raises(IntegrationError):
        JenkinsCollector(_cfg(source="jenkins", resource=None))


def test_jenkins_is_available_true_when_configured():
    c = JenkinsCollector(_cfg(source="jenkins"))
    assert c.is_available() is True


# ---------------------------------------------------------------------------
# Successful collection — GitHub Actions
# ---------------------------------------------------------------------------
def test_github_collect_returns_envelope(monkeypatch):
    runs_payload = json.dumps(
        {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 12345,
                    "name": "CI",
                    "head_branch": "main",
                    "head_sha": "abcdef",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-08-27T10:15:00Z",
                    "updated_at": "2026-08-27T10:25:00Z",
                    "html_url": "https://github.com/octocat/Hello-World/runs/12345",
                    "actor": {"login": "octocat"},
                }
            ],
        }
    ).encode("utf-8")
    jobs_payload = json.dumps(
        {
            "jobs": [
                {
                    "name": "build",
                    "conclusion": "failure",
                    "status": "completed",
                    "started_at": "2026-08-27T10:15:00Z",
                    "completed_at": "2026-08-27T10:25:00Z",
                    "steps": [
                        {
                            "name": "checkout",
                            "conclusion": "success",
                            "number": 1,
                        },
                        {
                            "name": "compile",
                            "conclusion": "failure",
                            "number": 2,
                        },
                    ],
                }
            ]
        }
    ).encode("utf-8")

    side_effect = _urlopen_factory([runs_payload, jobs_payload])
    monkeypatch.setenv("AUTORCA_TOKEN", "ghs_fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = GitHubActionsCollector(_cfg(source="github_actions"))
        result = collector.collect_with_metadata()
    assert len(result.items) == 1
    envelope = json.loads(result.items[0].raw_text)
    assert envelope["type"] == "github_actions_runs"
    assert envelope["run_count"] == 1
    assert int(envelope["runs"][0]["id"]) == 12345
    assert result.metadata["endpoint"] == "https://example.com"


# ---------------------------------------------------------------------------
# Successful collection — GitLab CI
# ---------------------------------------------------------------------------
def test_gitlab_collect_returns_envelope(monkeypatch):
    pipelines_payload = json.dumps(
        [
            {
                "id": 99,
                "iid": 7,
                "project_id": 1234,
                "sha": "deadbeef",
                "ref": "main",
                "status": "failed",
                "source": "push",
                "created_at": "2026-08-27T10:15:00Z",
                "updated_at": "2026-08-27T10:25:00Z",
                "web_url": "https://gitlab.example.com/mygroup/myproj/-/pipelines/99",
                "user": {"username": "developer"},
            }
        ]
    ).encode("utf-8")
    jobs_payload = json.dumps(
        [
            {
                "id": 1,
                "name": "build",
                "stage": "test",
                "status": "failed",
                "started_at": "2026-08-27T10:15:00Z",
                "finished_at": "2026-08-27T10:25:00Z",
                "failure_reason": "script exited with 1",
            }
        ]
    ).encode("utf-8")

    side_effect = _urlopen_factory([pipelines_payload, jobs_payload])
    monkeypatch.setenv("AUTORCA_TOKEN", "glpat_fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = GitLabCICollector(_cfg(source="gitlab_ci"))
        result = collector.collect_with_metadata()
    envelope = json.loads(result.items[0].raw_text)
    assert envelope["type"] == "gitlab_ci_pipelines"
    assert envelope["run_count"] == 1
    assert envelope["pipelines"][0]["id"] == 99
    assert "glpat_fakesecret" not in result.items[0].raw_text
    assert result.metadata["endpoint"] == "https://example.com"


# ---------------------------------------------------------------------------
# Successful collection — Jenkins
# ---------------------------------------------------------------------------
def test_jenkins_collect_returns_envelope(monkeypatch):
    job_payload = json.dumps(
        {
            "name": "my-pipeline",
            "url": "https://jenkins.example.com/job/my-pipeline/",
            "builds": [{"number": 42, "url": "https://jenkins/job/my-pipeline/42/"}],
        }
    ).encode("utf-8")
    build_payload = json.dumps(
        {
            "id": "42",
            "number": 42,
            "result": "FAILURE",
            "timestamp": 1756296000000,  # 2026-08-27T10:40:00Z
            "duration": 120000,
            "url": "https://jenkins/job/my-pipeline/42/",
            "fullDisplayName": "my-pipeline #42",
            "culprits": [{"fullName": "developer"}],
        }
    ).encode("utf-8")

    side_effect = _urlopen_factory([job_payload, build_payload])
    monkeypatch.setenv("AUTORCA_TOKEN", "user:apitoken")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = JenkinsCollector(_cfg(source="jenkins"))
        result = collector.collect_with_metadata()
    envelope = json.loads(result.items[0].raw_text)
    assert envelope["type"] == "jenkins_builds"
    assert envelope["run_count"] == 1
    assert envelope["builds"][0]["number"] == 42


# ---------------------------------------------------------------------------
# Time-window enforcement (GitHub Actions as the canonical example)
# ---------------------------------------------------------------------------
def test_github_filters_runs_outside_window(monkeypatch):
    runs_payload = json.dumps(
        {
            "total_count": 2,
            "workflow_runs": [
                {
                    "id": 1,
                    "name": "old",
                    "head_sha": "x",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": "2020-01-01T00:00:00Z",
                    "updated_at": "2020-01-01T00:01:00Z",
                    "html_url": "u",
                },
                {
                    "id": 2,
                    "name": "new",
                    "head_sha": "y",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-08-27T10:15:00Z",
                    "updated_at": "2026-08-27T10:25:00Z",
                    "html_url": "u",
                },
            ],
        }
    ).encode("utf-8")
    jobs_payload = json.dumps({"jobs": []}).encode("utf-8")

    side_effect = _urlopen_factory([runs_payload, jobs_payload, jobs_payload])
    monkeypatch.setenv("AUTORCA_TOKEN", "fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = GitHubActionsCollector(_cfg(source="github_actions"))
        result = collector.collect_with_metadata()
    envelope = json.loads(result.items[0].raw_text)
    names = [r["name"] for r in envelope["runs"]]
    assert "new" in names
    assert "old" not in names


# ---------------------------------------------------------------------------
# Authentication — secret never leaks
# ---------------------------------------------------------------------------
def test_github_authorization_header_never_appears_in_response(monkeypatch):
    runs_payload = json.dumps({"total_count": 0, "workflow_runs": []}).encode(
        "utf-8"
    )
    side_effect = _urlopen_factory([runs_payload])
    monkeypatch.setenv("AUTORCA_TOKEN", "supersecrettoken")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = GitHubActionsCollector(_cfg(source="github_actions"))
        result = collector.collect_with_metadata()
    text = result.items[0].raw_text
    assert "supersecrettoken" not in text
    assert "Bearer supersecrettoken" not in text


def test_gitlab_private_token_scrubbed_from_response(monkeypatch):
    pipelines_payload = json.dumps([]).encode("utf-8")
    side_effect = _urlopen_factory([pipelines_payload])
    monkeypatch.setenv("AUTORCA_TOKEN", "glpat_supersecret")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = GitLabCICollector(_cfg(source="gitlab_ci"))
        result = collector.collect_with_metadata()
    text = result.items[0].raw_text
    assert "glpat_supersecret" not in text


def test_jenkins_basic_auth_scrubbed_from_response(monkeypatch):
    job_payload = json.dumps(
        {"name": "j", "url": "u", "builds": []}
    ).encode("utf-8")
    side_effect = _urlopen_factory([job_payload])
    monkeypatch.setenv("AUTORCA_TOKEN", "user:apitoken_supersecret")
    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        collector = JenkinsCollector(_cfg(source="jenkins"))
        result = collector.collect_with_metadata()
    text = result.items[0].raw_text
    assert "apitoken_supersecret" not in text


# ---------------------------------------------------------------------------
# Failure modes — common to all three
# ---------------------------------------------------------------------------
def test_github_timeout_raises_integration_error(monkeypatch):
    def _side_effect(req, timeout=None):
        raise socket.timeout("gh timed out")

    monkeypatch.setenv("AUTORCA_TOKEN", "fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = GitHubActionsCollector(_cfg(source="github_actions"))
        with pytest.raises(IntegrationError):
            collector.collect_with_metadata()


def test_gitlab_http_500_raises_integration_error(monkeypatch):
    body = io.BytesIO(b'{"error":"server"}')

    def _side_effect(req, timeout=None):
        raise urllib.error.HTTPError(
            url="http://x", code=500, msg="err", hdrs=None, fp=body
        )

    monkeypatch.setenv("AUTORCA_TOKEN", "fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = GitLabCICollector(_cfg(source="gitlab_ci"))
        with pytest.raises(IntegrationError):
            collector.collect_with_metadata()


def test_jenkins_connection_refused_raises_integration_error(monkeypatch):
    def _side_effect(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = JenkinsCollector(_cfg(source="jenkins"))
        with pytest.raises(IntegrationError):
            collector.collect_with_metadata()


def test_github_malformed_json_raises_integration_error(monkeypatch):
    def _side_effect(req, timeout=None):
        return _FakeResponse(b"NOT_JSON")

    monkeypatch.setenv("AUTORCA_TOKEN", "fakesecret")
    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        collector = GitHubActionsCollector(_cfg(source="github_actions"))
        with pytest.raises(IntegrationError):
            collector.collect_with_metadata()
