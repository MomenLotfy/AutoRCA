"""UI / frontend tests.

These tests verify:
1. The static UI bundle is served by the HTTP handler.
2. The HTML contains every screen the Investigation Console needs.
3. The JavaScript references the v1 API endpoints the UI relies on.
4. The CSS includes the styles the UI requires.
5. End-to-end: a real investigation is reachable through the same paths the
   UI consumes — list, detail, evidence, timeline, graph, remediation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from web_app import AutoRCAHandler, reset_investigation_service

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = REPO_ROOT / "web" / "static"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORCA_WORKSPACE_ROOT", str(tmp_path))
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


def _get(url):
    try:
        with urllib.request.urlopen(url) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


# ---------------------------------------------------------------------------
# Static asset structure
# ---------------------------------------------------------------------------
def _read(name):
    return (STATIC_ROOT / name).read_text(encoding="utf-8")


def test_index_html_serves_all_required_screens():
    html = _read("index.html")
    # Required screens
    for marker in (
        'id="new-investigation-panel"',     # create investigation form
        'id="incidents-panel"',             # incident list
        'id="investigation-detail"',        # detail container
        'data-tab="overview"',
        'data-tab="evidence"',
        'data-tab="timeline"',
        'data-tab="graph"',
        'data-tab="hypothesis"',
        'data-tab="remediation"',
        'data-tab="raw"',                   # debug view
        'id="evidence-explorer"',
        'id="timeline-list"',
        'id="graph-canvas"',
        'id="hypothesis-list"',
        'id="remediation-content"',
        'id="detail-reasoning-chain"',
        'id="correlation-list"',
    ):
        assert marker in html, f"missing UI section: {marker}"


def test_app_js_references_v1_api_endpoints():
    js = _read("app.js")
    for endpoint in (
        "/api/v1/investigations",
        "/api/health",
    ):
        assert endpoint in js, f"app.js should reference {endpoint}"


def test_app_js_does_not_hardcode_results():
    """UI must not embed fake investigation results in JS source."""
    js = _read("app.js")
    # The placeholder text on the homepage for empty lists is allowed, but
    # anything that looks like a real investigation ID or commit SHA in JS
    # source is a smell.
    assert "INV-AR" not in js
    assert "Missing Environment Variable" not in js
    assert "FastAPI demo" not in js


def test_styles_css_includes_severity_and_graph_styles():
    css = _read("styles.css")
    for rule in (
        ".sev.critical",
        ".incident-row",
        ".evidence-item",
        ".timeline-event",
        ".graph-canvas",
        ".hypothesis",
        ".remediation-card",
        ".tab-panel.active",
    ):
        assert rule in css, f"styles.css missing rule: {rule}"


def test_index_html_handles_empty_state_and_error_state():
    html = _read("index.html")
    # Default messages for empty list / missing sections
    assert "No investigations yet" in html or "incidents-list" in html
    assert "REAL INCIDENT INPUT" in html  # legacy test marker


# ---------------------------------------------------------------------------
# Static asset serving via HTTP
# ---------------------------------------------------------------------------
def test_http_serves_index_html(server):
    status, body = _get(f"{server}/")
    assert status == 200
    assert "AutoRCA" in body
    assert "investigation-detail" in body


def test_http_serves_app_js(server):
    status, body = _get(f"{server}/app.js")
    assert status == 200
    assert "/api/v1/investigations" in body


def test_http_serves_styles_css(server):
    status, body = _get(f"{server}/styles.css")
    assert status == 200
    assert ".shell" in body


def test_http_rejects_unknown_static_path(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{server}/missing.txt")
    assert exc.value.code == 404


# ---------------------------------------------------------------------------
# End-to-end: UI consumes real API data
# ---------------------------------------------------------------------------
def _post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _create_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@local"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "app.py").write_text("import os\nprint(os.environ.get('PORT', '8000'))\n", encoding="utf-8")
    (path / ".env").write_text("PORT=8000\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=path, check=True)
    subprocess.run(["git", "add", "-f", ".env"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    # Second commit removes the variable — extractor should pick this up
    (path / ".env").write_text("DEBUG=true\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".env"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove PORT"], cwd=path, check=True)
    return path


def test_ui_consumes_real_investigation_from_real_pipeline(server, workspace):
    repo = _create_repo(workspace / "ui_repo")
    _, body = _post(
        f"{server}/api/v1/investigations",
        {"repo": str(repo), "environment": "production"},
    )

    # The UI list endpoint surfaces this investigation
    status, list_body = _get(f"{server}/api/v1/investigations")
    assert status == 200
    assert isinstance(list_body, dict), f"list endpoint returned non-dict: {list_body!r}"
    inv_ids = [item["investigation_id"] for item in list_body["investigations"]]
    assert body["investigation_id"] in inv_ids

    # The UI detail endpoint returns the same payload
    _, detail = _get(f"{server}/api/v1/investigations/{body['investigation_id']}")
    assert detail["investigation_id"] == body["investigation_id"]
    assert detail["repository"] == str(repo)
    assert detail["evidence"]

    # Every section the UI's tabbed layout consumes
    for section in ("evidence", "timeline", "graph", "remediation"):
        status, sec = _get(f"{server}/api/v1/investigations/{body['investigation_id']}/{section}")
        assert status == 200
        assert sec["section"] == section
        # Evidence is always available; others may be unavailable for some scenarios
        if section == "evidence":
            assert sec["available"] is True
            assert isinstance(sec["data"], list)