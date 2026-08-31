"""Phase 2.2 — GitHubChangeCollector + GitLabChangeCollector tests.

All tests mock ``urllib.request.urlopen`` so no real HTTP is made.
Both collectors share ``ChangeProvider``; the tests cover both the
provider-neutral and the source-specific code paths.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import socket
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.base import IncidentContext
from collectors.change_provider_base import (
    MAX_CHANGES_PER_WINDOW,
    ChangeEvent,
    ChangeProvider,
    clamp_window,
)
from collectors.gitlab_change_collector import GitLabChangeCollector
from collectors.github_change_collector import GitHubChangeCollector
from collectors.integration_base import (
    IntegrationConfig,
    IntegrationError,
)


# ---------------------------------------------------------------------------
# Common fixtures / helpers
# ---------------------------------------------------------------------------
def _github_config(**overrides):
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 27, 11, 0, 0, tzinfo=dt.timezone.utc)
    kwargs = dict(
        source="github_changes",
        endpoint="https://api.github.com",
        resource="octocat/Hello-World",
        incident_start=start,
        incident_end=end,
        size=10,
        timeout_seconds=2.0,
        auth_env=None,
    )
    kwargs.update(overrides)
    return IntegrationConfig(**kwargs)


def _gitlab_config(**overrides):
    start = dt.datetime(2026, 8, 27, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 27, 11, 0, 0, tzinfo=dt.timezone.utc)
    kwargs = dict(
        source="gitlab_changes",
        endpoint="https://gitlab.com",
        resource="mygroup/mysubgroup/project",
        incident_start=start,
        incident_end=end,
        size=10,
        timeout_seconds=2.0,
        auth_env=None,
    )
    kwargs.update(overrides)
    return IntegrationConfig(**kwargs)


def _fake_response(*, body_bytes, status=200):
    """Build a mock that mimics an ``http.client.HTTPResponse``.

    The collector calls ``read()`` twice per endpoint (once for the
    body, once to detect EOF). ``read`` returns ``body_bytes`` once and
    then ``b""`` forever, so a single response can be reused across
    multiple endpoints.
    """
    body = body_bytes
    response = mock.MagicMock()
    response.status = status

    state = {"count": 0}

    def _read(*args, **kwargs):
        if state["count"] == 0:
            state["count"] += 1
            return body
        return b""

    response.read.side_effect = _read
    response.close = mock.MagicMock()
    return response


# ---------------------------------------------------------------------------
# GitHubCollector
# ---------------------------------------------------------------------------
def test_github_constructor_rejects_wrong_source():
    cfg = IntegrationConfig(
        source="prometheus",
        endpoint="https://api.github.com",
        resource="octocat/Hello-World",
    )
    with pytest.raises(IntegrationError):
        GitHubChangeCollector(cfg)


def test_github_constructor_requires_resource():
    cfg = IntegrationConfig(
        source="github_changes",
        endpoint="https://api.github.com",
    )
    with pytest.raises(IntegrationError):
        GitHubChangeCollector(cfg)


def test_github_is_available_requires_resource_and_endpoint():
    cfg = _github_config()
    c = GitHubChangeCollector(cfg)
    assert c.is_available() is True
    object.__setattr__(cfg, "endpoint", "")
    assert c.is_available() is False


def test_github_successful_collection_returns_envelope():
    cfg = _github_config()
    collector = GitHubChangeCollector(cfg)
    commits_payload = [
        {
            "sha": "abc123",
            "commit": {
                "message": "fix: payment race\n\nDetail",
                "author": {"name": "octocat", "date": "2026-08-27T10:15:00Z"},
                "committer": {"name": "octocat", "date": "2026-08-27T10:15:00Z"},
            },
            "author": {"login": "octocat"},
            "html_url": "https://github.com/octocat/Hello-World/commit/abc123",
        }
    ]
    prs_payload = [
        {
            "number": 42,
            "title": "Add retry",
            "state": "closed",
            "merged_at": "2026-08-27T10:30:00Z",
            "updated_at": "2026-08-27T10:30:00Z",
            "user": {"login": "alice"},
            "html_url": "https://github.com/octocat/Hello-World/pull/42",
            "merge_commit_sha": "deadbeef",
            "head": {"ref": "feature/retry"},
            "base": {"ref": "main"},
        }
    ]

    responses = [
        _fake_response(body_bytes=json.dumps(commits_payload).encode("utf-8")),
        _fake_response(body_bytes=json.dumps(prs_payload).encode("utf-8")),
    ]

    def _side_effect(req, *args, **kwargs):
        return responses.pop(0)

    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        result = collector.collect_with_metadata()
    assert result.metadata["source"] == "github_changes"
    assert result.metadata["repo"] == "octocat/Hello-World"
    assert result.metadata["event_count"] >= 2
    assert len(result.items) == 1
    envelope = json.loads(result.items[0].raw_text)
    assert envelope["type"] == "github_changes"
    kinds = [e["kind"] for e in envelope["events"]]
    assert "commit" in kinds
    assert "pr" in kinds


def test_github_no_window_raises():
    cfg = IntegrationConfig(
        source="github_changes",
        endpoint="https://api.github.com",
        resource="octocat/Hello-World",
    )
    c = GitHubChangeCollector(cfg)
    with pytest.raises(IntegrationError):
        c.collect()


def test_github_http_401_raises():
    cfg = _github_config()
    collector = GitHubChangeCollector(cfg)
    err = urllib.error.HTTPError(
        url="https://api.github.com/repos/octocat/Hello-World/commits",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"message":"Bad credentials"}'),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_github_bearer_auth_attached():
    cfg = _github_config(auth_env="AUTORCA_GH_TOKEN", auth_scheme="bearer")
    collector = GitHubChangeCollector(cfg)
    captured = {}

    def _capture(req, *args, **kwargs):
        captured["headers"] = dict(req.headers)
        # Empty payload; we only care about the header.
        return _fake_response(body_bytes=b"[]")

    with mock.patch.dict(os.environ, {"AUTORCA_GH_TOKEN": "ghp_topsecret"}):
        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            collector.collect()
    # The secret IS attached to Authorization — that is by design —
    # but the test proves the env var is read and the header is set.
    assert captured["headers"].get("Authorization") == "Bearer ghp_topsecret"


def test_github_basic_auth_attached():
    cfg = _github_config(auth_env="AUTORCA_GH_BASIC")
    collector = GitHubChangeCollector(cfg)
    captured = {}

    def _capture(req, *args, **kwargs):
        captured["headers"] = dict(req.headers)
        return _fake_response(body_bytes=b"[]")

    with mock.patch.dict(os.environ, {"AUTORCA_GH_BASIC": "user:pass"}):
        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            collector.collect()
    auth = captured["headers"].get("Authorization", "")
    assert auth.startswith("Basic ")
    # The literal credentials must NOT appear in the header.
    assert "user:pass" not in auth


def test_github_secret_does_not_leak_into_envelope():
    cfg = _github_config(auth_env="AUTORCA_GH_TOKEN", auth_scheme="bearer")
    collector = GitHubChangeCollector(cfg)
    # Two endpoints are called (commits + PRs); the same response body
    # is reused for both.
    with mock.patch.dict(os.environ, {"AUTORCA_GH_TOKEN": "topsecret"}):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_fake_response(body_bytes=b"[]"),
        ):
            result = collector.collect_with_metadata()
    assert "topsecret" not in result.items[0].raw_text
    assert "topsecret" not in json.dumps(result.metadata)


def test_github_secret_scrubbed_from_error():
    cfg = _github_config()
    collector = GitHubChangeCollector(cfg)
    err = urllib.error.HTTPError(
        url="https://api.github.com/repos/octocat/Hello-World/commits",
        code=500,
        msg="Server Error",
        hdrs=None,
        fp=io.BytesIO(b"Authorization: bearer leakedsecret"),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError) as exc_info:
            collector.collect()
    msg = str(exc_info.value)
    assert "leakedsecret" not in msg


def test_github_malformed_json_raises():
    cfg = _github_config()
    collector = GitHubChangeCollector(cfg)
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_fake_response(body_bytes=b"<<not json>>"),
    ):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_github_connection_refused_raises():
    cfg = _github_config()
    collector = GitHubChangeCollector(cfg)
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(("ConnectionRefusedError", "refused")),
    ):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_github_changes_provider_protocol():
    cfg = _github_config()
    collector = GitHubChangeCollector(cfg)
    assert isinstance(collector, ChangeProvider)
    assert collector.name == "github_changes"


# ---------------------------------------------------------------------------
# GitLabCollector
# ---------------------------------------------------------------------------
def test_gitlab_constructor_rejects_wrong_source():
    cfg = IntegrationConfig(
        source="github_changes",
        endpoint="https://gitlab.com",
        resource="mygroup/project",
    )
    with pytest.raises(IntegrationError):
        GitLabChangeCollector(cfg)


def test_gitlab_constructor_requires_resource():
    cfg = IntegrationConfig(
        source="gitlab_changes",
        endpoint="https://gitlab.com",
    )
    with pytest.raises(IntegrationError):
        GitLabChangeCollector(cfg)


def test_gitlab_is_available_requires_resource_and_endpoint():
    cfg = _gitlab_config()
    c = GitLabChangeCollector(cfg)
    assert c.is_available() is True
    object.__setattr__(cfg, "endpoint", "")
    assert c.is_available() is False


def test_gitlab_successful_collection_returns_envelope():
    cfg = _gitlab_config()
    collector = GitLabChangeCollector(cfg)
    commits_payload = [
        {
            "id": "abc1234567",
            "short_id": "abc1234",
            "title": "fix: rollback handler",
            "author_name": "alice",
            "author": {
                "username": "alice",
                "name": "Alice",
                "date": "2026-08-27T10:15:00.000Z",
            },
            "committer": {"date": "2026-08-27T10:15:00.000Z"},
            "web_url": "https://gitlab.com/mygroup/project/-/commit/abc1234567",
        }
    ]
    mrs_payload = [
        {
            "iid": 7,
            "title": "Add rate limit",
            "state": "merged",
            "updated_at": "2026-08-27T10:30:00Z",
            "source_branch": "feature/rate",
            "target_branch": "main",
            "merge_commit_sha": "deadbeef",
            "author": {"username": "alice"},
            "web_url": "https://gitlab.com/mygroup/project/-/merge_requests/7",
        }
    ]

    responses = [
        _fake_response(body_bytes=json.dumps(commits_payload).encode("utf-8")),
        _fake_response(body_bytes=json.dumps(mrs_payload).encode("utf-8")),
    ]

    def _side_effect(req, *args, **kwargs):
        return responses.pop(0)

    with mock.patch("urllib.request.urlopen", side_effect=_side_effect):
        result = collector.collect_with_metadata()
    envelope = json.loads(result.items[0].raw_text)
    assert envelope["type"] == "gitlab_changes"
    assert envelope["project"] == "mygroup/mysubgroup/project"
    kinds = [e["kind"] for e in envelope["events"]]
    assert "commit" in kinds
    assert "merge_request" in kinds


def test_gitlab_no_window_raises():
    cfg = IntegrationConfig(
        source="gitlab_changes",
        endpoint="https://gitlab.com",
        resource="mygroup/project",
    )
    c = GitLabChangeCollector(cfg)
    with pytest.raises(IntegrationError):
        c.collect()


def test_gitlab_http_401_raises():
    cfg = _gitlab_config()
    collector = GitLabChangeCollector(cfg)
    err = urllib.error.HTTPError(
        url="https://gitlab.com/api/v4/projects/x/commits",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"message":"401 Unauthorized"}'),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_gitlab_private_token_attached():
    cfg = _gitlab_config(auth_env="AUTORCA_GL_TOKEN")
    collector = GitLabChangeCollector(cfg)
    captured = {}

    def _capture(req, *args, **kwargs):
        captured["headers"] = dict(req.headers)
        return _fake_response(body_bytes=b"[]")

    with mock.patch.dict(os.environ, {"AUTORCA_GL_TOKEN": "glpat-tops3cret"}):
        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            collector.collect()
    # urllib normalizes header keys to lowercase (capitalization is
    # title-cased in the request, normalized in the captured dict).
    token_value = (
        captured["headers"].get("PRIVATE-TOKEN")
        or captured["headers"].get("Private-token")
        or captured["headers"].get("private-token")
    )
    assert token_value == "glpat-tops3cret"


def test_gitlab_secret_does_not_leak_into_envelope():
    cfg = _gitlab_config(auth_env="AUTORCA_GL_TOKEN")
    collector = GitLabChangeCollector(cfg)
    with mock.patch.dict(os.environ, {"AUTORCA_GL_TOKEN": "topsecret"}):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_fake_response(body_bytes=b"[]"),
        ):
            result = collector.collect_with_metadata()
    assert "topsecret" not in result.items[0].raw_text
    assert "topsecret" not in json.dumps(result.metadata)


def test_gitlab_secret_scrubbed_from_error():
    cfg = _gitlab_config()
    collector = GitLabChangeCollector(cfg)
    err = urllib.error.HTTPError(
        url="https://gitlab.com/api/v4/projects/x/commits",
        code=500,
        msg="Server Error",
        hdrs=None,
        fp=io.BytesIO(b"PRIVATE-TOKEN: leakedsecret"),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(IntegrationError) as exc_info:
            collector.collect()
    msg = str(exc_info.value)
    assert "leakedsecret" not in msg


def test_gitlab_malformed_json_raises():
    cfg = _gitlab_config()
    collector = GitLabChangeCollector(cfg)
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_fake_response(body_bytes=b"<<not json>>"),
    ):
        with pytest.raises(IntegrationError):
            collector.collect()


def test_gitlab_changes_provider_protocol():
    cfg = _gitlab_config()
    collector = GitLabChangeCollector(cfg)
    assert isinstance(collector, ChangeProvider)
    assert collector.name == "gitlab_changes"


# ---------------------------------------------------------------------------
# ChangeProvider — clamp_window
# ---------------------------------------------------------------------------
def test_clamp_window_rejects_fully_open_window():
    with pytest.raises(ValueError):
        clamp_window(None, None)


def test_clamp_window_rejects_reversed_window():
    since = dt.datetime(2026, 8, 27, 11, tzinfo=dt.timezone.utc)
    until = dt.datetime(2026, 8, 27, 10, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError):
        clamp_window(since, until)


def test_clamp_window_extends_short_windows_to_minimum():
    since = dt.datetime(2026, 8, 27, 10, tzinfo=dt.timezone.utc)
    until = since + dt.timedelta(seconds=5)
    new_since, new_until = clamp_window(since, until)
    assert (new_until - new_since).total_seconds() >= 60


def test_clamp_window_caps_long_windows():
    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    until = since + dt.timedelta(days=30)
    _, new_until = clamp_window(since, until)
    assert (new_until - since).days <= 7


# ---------------------------------------------------------------------------
# ChangeProvider — ChangeEvent
# ---------------------------------------------------------------------------
def test_change_event_to_dict_is_stable():
    e = ChangeEvent(
        id="x",
        kind="commit",
        title="t",
        author="a",
        timestamp="2026-08-27T10:00:00Z",
        url="u",
        ref="main",
        sha="sha",
        extra={"merged": True},
    )
    d = e.to_dict()
    assert d == {
        "id": "x",
        "kind": "commit",
        "title": "t",
        "author": "a",
        "timestamp": "2026-08-27T10:00:00Z",
        "url": "u",
        "ref": "main",
        "sha": "sha",
        "extra": {"merged": True},
    }


# ---------------------------------------------------------------------------
# Extractor round-trips
# ---------------------------------------------------------------------------
def test_github_extractor_produces_observations():
    from extractors.base import ExtractionContext, ObservationIdGenerator
    import extractors.github_change_extractor as ghe

    envelope = {
        "type": "github_changes",
        "repo": "octocat/Hello-World",
        "service": "payment-api",
        "incident_start": "2026-08-27T10:00:00+00:00",
        "incident_end": "2026-08-27T11:00:00+00:00",
        "event_count": 1,
        "events": [
            {
                "id": "abc123",
                "kind": "commit",
                "title": "fix: payment race",
                "author": "octocat",
                "timestamp": "2026-08-27T10:15:00Z",
                "url": "https://github.com/o/r/commit/abc123",
                "sha": "abc123",
                "extra": {},
            }
        ],
    }
    ctx = ExtractionContext(
        analysis_id="AR20260827-100000",
        raw_content=json.dumps(envelope),
        id_generator=ObservationIdGenerator(),
    )
    obs_list = ghe.GitHubChangeExtractor().extract(ctx)
    assert len(obs_list) == 1
    assert obs_list[0].source == "github_changes"
    assert obs_list[0].kind == "generic_log_line"
    assert obs_list[0].resource == "github:octocat/Hello-World"
    assert obs_list[0].service == "payment-api"
    assert obs_list[0].data["title"] == "fix: payment race"
    assert obs_list[0].data["author"] == "octocat"


def test_gitlab_extractor_produces_observations():
    from extractors.base import ExtractionContext, ObservationIdGenerator
    import extractors.gitlab_change_extractor as gle

    envelope = {
        "type": "gitlab_changes",
        "project": "mygroup/mysubgroup/project",
        "service": "billing",
        "incident_start": "2026-08-27T10:00:00+00:00",
        "incident_end": "2026-08-27T11:00:00+00:00",
        "event_count": 1,
        "events": [
            {
                "id": "7",
                "kind": "merge_request",
                "title": "Add rate limit",
                "author": "alice",
                "timestamp": "2026-08-27T10:30:00Z",
                "url": "https://gitlab.com/mygroup/project/-/merge_requests/7",
                "sha": "deadbeef",
                "ref": "feature/rate",
                "extra": {"merged": True, "state": "merged"},
            }
        ],
    }
    ctx = ExtractionContext(
        analysis_id="AR20260827-100000",
        raw_content=json.dumps(envelope),
        id_generator=ObservationIdGenerator(),
    )
    obs_list = gle.GitLabChangeExtractor().extract(ctx)
    assert len(obs_list) == 1
    assert obs_list[0].source == "gitlab_changes"
    assert obs_list[0].kind == "generic_log_line"
    assert obs_list[0].resource == "gitlab:mygroup/mysubgroup/project"
    assert obs_list[0].service == "billing"
    assert obs_list[0].data["title"] == "Add rate limit"
    assert obs_list[0].data["merged"] is True


def test_github_extractor_handles_empty_envelope():
    from extractors.base import ExtractionContext, ObservationIdGenerator
    import extractors.github_change_extractor as ghe

    ctx = ExtractionContext(
        analysis_id="AR20260827-100000",
        raw_content="",
        id_generator=ObservationIdGenerator(),
    )
    assert ghe.GitHubChangeExtractor().extract(ctx) == []


def test_gitlab_extractor_handles_empty_envelope():
    from extractors.base import ExtractionContext, ObservationIdGenerator
    import extractors.gitlab_change_extractor as gle

    ctx = ExtractionContext(
        analysis_id="AR20260827-100000",
        raw_content="",
        id_generator=ObservationIdGenerator(),
    )
    assert gle.GitLabChangeExtractor().extract(ctx) == []


# ---------------------------------------------------------------------------
# Registry / pipeline integration
# ---------------------------------------------------------------------------
def test_registry_includes_github_and_gitlab():
    from extractors.registry import registry

    ids = {m.extractor_id for m in registry.all_metadata()}
    assert "github_change_extractor" in ids
    assert "gitlab_change_extractor" in ids


def test_pipeline_registers_github_and_gitlab_extractors():
    import extractors.github_change_extractor  # noqa: F401
    import extractors.gitlab_change_extractor  # noqa: F401

    from extractors.registry import registry

    gh = registry.get_extractor_classes_for_source("github_changes")
    gl = registry.get_extractor_classes_for_source("gitlab_changes")
    assert any(cls.EXTRACTOR_ID == "github_change_extractor" for cls in gh)
    assert any(cls.EXTRACTOR_ID == "gitlab_change_extractor" for cls in gl)