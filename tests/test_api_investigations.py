"""HTTP-level tests for the /api/v1/investigations endpoints.

These tests use the same stdlib test-server pattern as ``test_web_app.py`` —
a ``ThreadingHTTPServer`` running on a random port — to exercise the real
HTTP handler without any external framework.

All tests build their own temporary git repository on disk and run the real
``AnalysisPipeline`` end-to-end. No mocks are used for the pipeline.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from web_app import AutoRCAHandler, reset_investigation_service
from api.investigation_service import InvestigationService
from pipeline import AnalysisPipeline


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared workspace — set the env var BEFORE the HTTP server starts in each
# test so all subprocess modules see the same root.
# ---------------------------------------------------------------------------
@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORCA_WORKSPACE_ROOT", str(tmp_path))
    # Drop the cached investigation service so the next request rebuilds the
    # pipeline against the freshly-set workspace root.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _create_repo(path: Path) -> Path:
    """Create a tiny git repository with two commits so ``git diff HEAD~1`` works."""
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

    # Second commit introduces a configuration regression that the extractor can spot.
    (repo / ".env").write_text("DEBUG=true\n", encoding="utf-8")
    _git(repo, "add", "-f", ".env")
    _git(repo, "commit", "-q", "-m", "remove required PORT variable")
    return repo


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def test_health_endpoint_returns_ok(server):
    status, body = _get(f"{server}/api/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["service"] == "autorca-web"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------
def test_create_investigation_requires_real_repository(server, workspace):
    status, body = _post(f"{server}/api/v1/investigations", {})
    assert status == 400
    assert "repository" in body["error"].lower()


def test_create_investigation_rejects_invalid_environment(server, workspace):
    status, body = _post(
        f"{server}/api/v1/investigations",
        {"repo": str(workspace), "environment": "qa"},
    )
    assert status == 400
    assert "environment" in body["error"].lower()


def test_create_investigation_rejects_path_outside_workspace(server, workspace):
    status, body = _post(
        f"{server}/api/v1/investigations",
        {"repo": "/etc/passwd", "environment": "local"},
    )
    assert status == 400
    assert "workspace" in body["error"].lower()


# ---------------------------------------------------------------------------
# Happy path against a real scenario repository
# ---------------------------------------------------------------------------
def test_create_investigation_returns_real_pipeline_result(server, workspace):
    repo_path = _create_repo(workspace / "incident_repo")

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo_path),
            "environment": "production",
            "full_name": "org/incident_repo",
        },
    )
    assert status == 201, body
    assert body["status"] in {"completed", "no_root_cause"}
    assert body["environment"] == "production"
    assert body["repository_full_name"] == "org/incident_repo"
    assert isinstance(body["evidence"], list)
    assert isinstance(body["observations"], list)
    assert isinstance(body["hypotheses"], list)
    assert body["timeline"] is not None
    assert body["graph"] is not None


def test_get_investigation_returns_full_payload(server, workspace):
    repo_path = _create_repo(workspace / "incident_repo_full")
    _, created = _post(
        f"{server}/api/v1/investigations",
        {"repo": str(repo_path), "environment": "production"},
    )
    investigation_id = created["investigation_id"]

    status, body = _get(f"{server}/api/v1/investigations/{investigation_id}")
    assert status == 200
    assert body["investigation_id"] == investigation_id
    assert body["repository_full_name"]


def test_get_investigation_not_found(server):
    status, body = _get(f"{server}/api/v1/investigations/INV-DOES-NOT-EXIST")
    assert status == 404
    assert "not found" in body["error"].lower()


def test_list_investigations_returns_recent_first(server, workspace):
    repo_a = _create_repo(workspace / "list_repo_a")
    repo_b = _create_repo(workspace / "list_repo_b")

    _, inv_a = _post(
        f"{server}/api/v1/investigations",
        {"repo": str(repo_a), "environment": "production"},
    )
    _, inv_b = _post(
        f"{server}/api/v1/investigations",
        {"repo": str(repo_b), "environment": "production"},
    )

    status, body = _get(f"{server}/api/v1/investigations")
    assert status == 200
    ids = [item["investigation_id"] for item in body["investigations"]]
    assert inv_a["investigation_id"] in ids
    assert inv_b["investigation_id"] in ids
    # Most recent first ordering
    assert ids.index(inv_b["investigation_id"]) < ids.index(inv_a["investigation_id"])
    # Each item carries the summary fields the UI needs
    item = body["investigations"][0]
    for key in (
        "investigation_id",
        "status",
        "environment",
        "repository",
        "root_cause",
        "confidence",
        "severity",
    ):
        assert key in item


# ---------------------------------------------------------------------------
# Section endpoints
# ---------------------------------------------------------------------------
def _make_investigation(server, workspace):
    repo_path = _create_repo(workspace / "section_repo")
    _, body = _post(
        f"{server}/api/v1/investigations",
        {"repo": str(repo_path), "environment": "production"},
    )
    return body


def test_section_evidence_returns_array(server, workspace):
    inv = _make_investigation(server, workspace)
    status, body = _get(f"{server}/api/v1/investigations/{inv['investigation_id']}/evidence")
    assert status == 200
    assert body["section"] == "evidence"
    assert body["available"] is True
    assert isinstance(body["data"], list)


def test_section_timeline_returns_object(server, workspace):
    inv = _make_investigation(server, workspace)
    status, body = _get(f"{server}/api/v1/investigations/{inv['investigation_id']}/timeline")
    assert status == 200
    assert body["section"] == "timeline"
    assert isinstance(body["data"], dict)
    assert "events" in body["data"]


def test_section_graph_returns_object(server, workspace):
    inv = _make_investigation(server, workspace)
    status, body = _get(f"{server}/api/v1/investigations/{inv['investigation_id']}/graph")
    assert status == 200
    assert body["section"] == "graph"
    graph = body["data"]
    assert "nodes" in graph
    assert "edges" in graph


def test_section_remediation_or_unavailable(server, workspace):
    inv = _make_investigation(server, workspace)
    status, body = _get(f"{server}/api/v1/investigations/{inv['investigation_id']}/remediation")
    assert status == 200
    if body["available"]:
        rem = body["data"]
        assert "action" in rem
        assert "steps" in rem
    else:
        assert body["data"] is None


def test_section_fingerprint_returns_object(server, workspace):
    inv = _make_investigation(server, workspace)
    status, body = _get(f"{server}/api/v1/investigations/{inv['investigation_id']}/fingerprint")
    assert status == 200
    assert body["section"] == "fingerprint"
    if body["available"]:
        assert "failure_category" in body["data"]


def test_section_for_unknown_investigation_returns_404(server):
    status, body = _get(f"{server}/api/v1/investigations/INV-MISSING/evidence")
    assert status == 404


# ---------------------------------------------------------------------------
# Real FastAPI incident scenario (the canonical demo)
# ---------------------------------------------------------------------------
def test_real_fastapi_demo_missing_env_variable_incident(server, workspace):
    """End-to-end: missing env var incident from the canonical target repo."""
    target = REPO_ROOT / "projects-for-test" / "python-fastapi-demo-docker"
    if not target.exists():
        pytest.skip("target demo repo not present in this checkout")

    # Make the target repo visible inside the test workspace by symlinking it.
    # Tests must not mutate the target repo, so we never write to it.
    symlink_path = workspace / "demo_target"
    symlink_path.symlink_to(target, target_is_directory=True)

    traceback = (
        "Traceback (most recent call last):\n"
        "  File \"/server/app/main.py\", line 1, in <module>\n"
        "    from app.connect import DATABASE_URL\n"
        "  File \"/server/app/connect.py\", line 4, in <module>\n"
        "    DATABASE_URL = os.environ['DOCKER_DATABASE_URL']\n"
        "  File \"/usr/local/lib/python3.11/os.py\", line 680, in __getitem__\n"
        "    raise KeyError(key) from None\n"
        "KeyError: 'DOCKER_DATABASE_URL'\n"
    )
    traceback_file = workspace / "traceback.txt"
    traceback_file.write_text(traceback)

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(symlink_path),
            "environment": "production",
            "traceback": str(traceback_file),
            "no_diff": True,  # Don't mutate the target repo by triggering a real diff
        },
    )
    assert status == 201, body
    assert body["status"] in {"completed", "no_root_cause"}

    selected = body["selected_hypothesis"]
    if selected is not None:
        assert selected["failure_type_id"].startswith("FT")
        assert selected["status"] == "selected"
        assert isinstance(body["incident_summary"]["confidence"], (int, float))
        assert body["evidence"]


# ---------------------------------------------------------------------------
# Direct unit-tests of the service layer (no HTTP)
# ---------------------------------------------------------------------------
def test_service_persists_investigation(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORCA_WORKSPACE_ROOT", str(tmp_path))
    pipeline = AnalysisPipeline.from_config_files(
        REPO_ROOT / "rules" / "rules.config.json",
        REPO_ROOT / "taxonomy" / "taxonomy.yaml",
    )
    service = InvestigationService(pipeline)
    repo_path = _create_repo(tmp_path / "svc_repo")
    investigation = service.create_investigation(
        {"repo": str(repo_path), "environment": "staging"}
    )
    assert investigation.investigation_id.startswith("INV-")
    assert investigation.environment == "staging"
    assert investigation.payload["investigation_id"] == investigation.investigation_id

    items = service.list_investigations()
    assert any(item.investigation_id == investigation.investigation_id for item in items)
    again = service.get_investigation(investigation.investigation_id)
    assert again is investigation