"""Phase 2.2 — API extension tests for Prometheus + GitHub + GitLab.

The same ``POST /api/v1/investigations`` endpoint now accepts three
new optional blocks: ``prometheus``, ``github_changes``,
``gitlab_changes``. The collectors are mocked so no real HTTP is made.

These tests mirror the Phase 2.1 ``tests/test_phase2_api_extension.py``
pattern.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from web_app import AutoRCAHandler, reset_investigation_service  # noqa: E402

from collectors.base import CollectedItem  # noqa: E402
from collectors.integration_base import (  # noqa: E402
    IntegrationError,
    IntegrationResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORCA_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTORCA_ES_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("AUTORCA_PROM_ALLOW_LOOPBACK", "1")
    reset_investigation_service()
    yield tmp_path
    reset_investigation_service()


@pytest.fixture()
def server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), AutoRCAHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        reset_investigation_service()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@local"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _create_repo(path: Path) -> Path:
    repo = path
    _init_repo(repo)
    (repo / "app.py").write_text(
        "import os\n"
        "import sys\n"
        "value = os.environ.get('PORT', '8000')\n"
        "print('started on', value)\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text("PORT=8000\nDEBUG=true\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "add", "-f", ".env")
    _git(repo, "commit", "-q", "-m", "initial commit")
    # Second commit removes the PORT env var so the port-conflict
    # extractor fires on the diff (mirrors the Phase 2.1 test pattern
    # so a repo with no other incident data still produces observations).
    (repo / ".env").write_text("DEBUG=true\n", encoding="utf-8")
    _git(repo, "add", "-f", ".env")
    _git(repo, "commit", "-q", "-m", "remove required PORT variable")
    return repo


def _make_envelope(source: str, payload: dict) -> CollectedItem:
    envelope = {"type": source, **payload}
    return CollectedItem(
        source=source,
        raw_text=json.dumps(envelope),
        timestamp="2026-08-27T10:30:00+00:00",
        metadata={"hit_count": 1},
    )


# ---------------------------------------------------------------------------
# Prometheus block runs the collector and surfaces in payload
# ---------------------------------------------------------------------------
def test_prometheus_block_runs_collector_and_surfaces_in_payload(
    server, workspace, monkeypatch
):
    from collectors.prometheus_collector import PrometheusCollector

    repo = _create_repo(workspace / "prom_repo")
    item = _make_envelope(
        "prometheus",
        {
            "type": "prometheus_query",
            "mode": "range",
            "query": "up",
            "metric_name": "up",
            "service": "prometheus",
            "series_count": 1,
            "sample_count": 1,
            "series": [
                {
                    "labels": {
                        "__name__": "up",
                        "job": "prometheus",
                        "service": "prometheus",
                    },
                    "value": [1726500000.0, 1.0],
                }
            ],
        },
    )

    def fake_collect_with_metadata(self, ctx=None):
        return IntegrationResult(
            items=(item,),
            metadata={
                "source": "prometheus",
                "endpoint": self._config.endpoint,
                "mode": "range",
                "query": "up",
                "sample_count": 1,
                "series_count": 1,
            },
        )

    monkeypatch.setattr(
        PrometheusCollector,
        "collect_with_metadata",
        fake_collect_with_metadata,
    )

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "prometheus": {
                "url": "http://127.0.0.1:9090",
                "query": "up",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 201, body
    assert "prometheus" in body
    p_block = body["prometheus"]
    assert p_block["endpoint"] == "http://127.0.0.1:9090"
    assert p_block["query"] == "up"
    assert p_block["sample_count"] == 1

    prom_obs = [o for o in body["observations"] if o["source"] == "prometheus"]
    assert len(prom_obs) >= 1
    assert prom_obs[0]["kind"] == "generic_log_line"


# ---------------------------------------------------------------------------
# GitHub block runs the collector and surfaces in payload
# ---------------------------------------------------------------------------
def test_github_block_runs_collector_and_surfaces_in_payload(
    server, workspace, monkeypatch
):
    from collectors.github_change_collector import GitHubChangeCollector

    repo = _create_repo(workspace / "gh_repo")
    item = _make_envelope(
        "github_changes",
        {
            "repo": "octocat/Hello-World",
            "service": None,
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
        },
    )

    def fake_collect_with_metadata(self, ctx=None):
        return IntegrationResult(
            items=(item,),
            metadata={
                "source": "github_changes",
                "endpoint": self._config.endpoint,
                "repo": "octocat/Hello-World",
                "event_count": 1,
            },
        )

    monkeypatch.setattr(
        GitHubChangeCollector,
        "collect_with_metadata",
        fake_collect_with_metadata,
    )

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "github_changes": {
                "url": "https://api.github.com",
                "resource": "octocat/Hello-World",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 201, body
    assert "github_changes" in body
    gh_block = body["github_changes"]
    assert gh_block["endpoint"] == "https://api.github.com"
    assert gh_block["repo"] == "octocat/Hello-World"

    gh_obs = [o for o in body["observations"] if o["source"] == "github_changes"]
    assert len(gh_obs) >= 1
    assert gh_obs[0]["data"]["title"] == "fix: payment race"


# ---------------------------------------------------------------------------
# GitLab block runs the collector and surfaces in payload
# ---------------------------------------------------------------------------
def test_gitlab_block_runs_collector_and_surfaces_in_payload(
    server, workspace, monkeypatch
):
    from collectors.gitlab_change_collector import GitLabChangeCollector

    repo = _create_repo(workspace / "gl_repo")
    item = _make_envelope(
        "gitlab_changes",
        {
            "project": "mygroup/mysubgroup/project",
            "service": None,
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
                    "url": "https://gitlab.com/m/p/-/merge_requests/7",
                    "sha": "deadbeef",
                    "ref": "feature/rate",
                    "extra": {"merged": True},
                }
            ],
        },
    )

    def fake_collect_with_metadata(self, ctx=None):
        return IntegrationResult(
            items=(item,),
            metadata={
                "source": "gitlab_changes",
                "endpoint": self._config.endpoint,
                "project": "mygroup/mysubgroup/project",
                "event_count": 1,
            },
        )

    monkeypatch.setattr(
        GitLabChangeCollector,
        "collect_with_metadata",
        fake_collect_with_metadata,
    )

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "gitlab_changes": {
                "url": "https://gitlab.com",
                "resource": "mygroup/mysubgroup/project",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 201, body
    assert "gitlab_changes" in body
    gl_block = body["gitlab_changes"]
    assert gl_block["project"] == "mygroup/mysubgroup/project"

    gl_obs = [o for o in body["observations"] if o["source"] == "gitlab_changes"]
    assert len(gl_obs) >= 1
    assert gl_obs[0]["data"]["title"] == "Add rate limit"


# ---------------------------------------------------------------------------
# None of the Phase 2.2 blocks → behaviour unchanged
# ---------------------------------------------------------------------------
def test_no_phase22_blocks_preserves_phase1_behaviour(server, workspace):
    repo = _create_repo(workspace / "no_phase22_repo")
    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
        },
    )
    assert status == 201, body
    for key in ("prometheus", "github_changes", "gitlab_changes"):
        assert key not in body
    assert all(
        o["source"] not in ("prometheus", "github_changes", "gitlab_changes")
        for o in body["observations"]
    )


# ---------------------------------------------------------------------------
# Missing required query for Prometheus → 400
# ---------------------------------------------------------------------------
def test_prometheus_missing_query_returns_400(server, workspace):
    repo = _create_repo(workspace / "prom_missing_repo")
    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "prometheus": {
                "url": "http://127.0.0.1:9090",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 400
    assert "query" in body["error"].lower()


# ---------------------------------------------------------------------------
# Missing required resource for GitHub → 400
# ---------------------------------------------------------------------------
def test_github_missing_resource_returns_400(server, workspace):
    repo = _create_repo(workspace / "gh_missing_repo")
    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "github_changes": {
                "url": "https://api.github.com",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 400
    assert "resource" in body["error"].lower()


# ---------------------------------------------------------------------------
# Integration error from the collector → 400
# ---------------------------------------------------------------------------
def test_prometheus_collector_failure_returns_400(
    server, workspace, monkeypatch
):
    from collectors.prometheus_collector import PrometheusCollector

    repo = _create_repo(workspace / "prom_fail_repo")

    def fake_collect_with_metadata(self, ctx=None):
        raise IntegrationError("simulated prometheus outage")

    monkeypatch.setattr(
        PrometheusCollector,
        "collect_with_metadata",
        fake_collect_with_metadata,
    )

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "prometheus": {
                "url": "http://127.0.0.1:9090",
                "query": "up",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 400
    assert "prometheus" in body["error"].lower()


# ---------------------------------------------------------------------------
# Authorization override via extra_headers → 400
# ---------------------------------------------------------------------------
def test_extra_headers_cannot_override_authorization_prometheus(
    server, workspace
):
    repo = _create_repo(workspace / "prom_hdr_repo")
    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "prometheus": {
                "url": "http://127.0.0.1:9090",
                "query": "up",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
                "extra_headers": {"Authorization": "Bearer stolen"},
            },
        },
    )
    assert status == 400
    assert "authorization" in body["error"].lower()