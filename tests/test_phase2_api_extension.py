"""Phase 2.1 — API extension tests.

The same ``POST /api/v1/investigations`` endpoint accepts an
``elasticsearch`` block in the JSON body. When present, the service
runs the collector and merges the result into ``sources``. When
absent, behaviour is unchanged.

These tests mock ``ElasticsearchCollector.collect_with_metadata`` so
no real HTTP call is made.
"""
from __future__ import annotations

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

from collectors.elasticsearch_collector import (  # noqa: E402
    ElasticsearchCollector,
)
from collectors.integration_base import (  # noqa: E402
    IntegrationError,
    IntegrationResult,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror the canonical pattern from test_api_investigations.py)
# ---------------------------------------------------------------------------
@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORCA_WORKSPACE_ROOT", str(tmp_path))
    # Tests use 127.0.0.1 fixtures; enable the loopback override.
    monkeypatch.setenv("AUTORCA_ES_ALLOW_LOOPBACK", "1")
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
    (repo / ".env").write_text("DEBUG=true\n", encoding="utf-8")
    _git(repo, "add", "-f", ".env")
    _git(repo, "commit", "-q", "-m", "remove required PORT variable")
    return repo


def _mock_collector_items(envelope: dict) -> str:
    from collectors.base import CollectedItem

    item = CollectedItem(
        source="elasticsearch",
        raw_text=json.dumps(envelope),
        timestamp="2026-08-27T10:30:00+00:00",
        metadata={"hit_count": 1, "index_pattern": "logs-*"},
    )
    return item


# ---------------------------------------------------------------------------
# ES block produces an investigation carrying the elasticsearch metadata
# ---------------------------------------------------------------------------
def test_elasticsearch_block_runs_collector_and_surfaces_in_payload(
    server, workspace, monkeypatch
):
    """When the request body includes an ``elasticsearch`` block, the
    API calls the collector, merges its envelope into ``sources``, and
    surfaces the collector metadata in ``payload['elasticsearch']``."""
    repo = _create_repo(workspace / "es_repo")

    envelope = {
        "type": "elasticsearch_search",
        "index_pattern": "logs-*",
        "service": "payment-api",
        "incident_start": "2026-08-27T10:00:00+00:00",
        "incident_end": "2026-08-27T11:00:00+00:00",
        "size": 10,
        "hit_count": 1,
        "status": 200,
        "hits": [
            {
                "index": "logs-2026.08.27",
                "id": "doc-1",
                "timestamp": "2026-08-27T10:30:00Z",
                "message": "OutOfMemoryError",
                "service": "payment-api",
                "level": "ERROR",
            }
        ],
    }
    item = _mock_collector_items(envelope)

    captured_meta = {}

    def fake_collect_with_metadata(self, ctx=None):
        captured_meta["endpoint"] = self._config.endpoint
        captured_meta["index_pattern"] = self._config.index_pattern
        captured_meta["service"] = self._config.service
        return IntegrationResult(items=(item,), metadata={
            "source": "elasticsearch",
            "endpoint": self._config.endpoint,
            "index_pattern": self._config.index_pattern,
            "service": self._config.service,
            "size": self._config.size,
            "hit_count": 1,
        })

    monkeypatch.setattr(
        ElasticsearchCollector,
        "collect_with_metadata",
        fake_collect_with_metadata,
    )
    monkeypatch.setenv("AUTORCA_ES_ALLOW_LOOPBACK", "1")

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "elasticsearch": {
                "url": "http://127.0.0.1:9200",
                "index_pattern": "logs-*",
                "service": "payment-api",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
                "size": 5,
            },
        },
    )
    assert status == 201, body

    # The collector was invoked with the validated URL and config.
    assert captured_meta["endpoint"] == "http://127.0.0.1:9200"
    assert captured_meta["index_pattern"] == "logs-*"
    assert captured_meta["service"] == "payment-api"

    # Payload carries an elasticsearch summary block.
    assert "elasticsearch" in body
    es_block = body["elasticsearch"]
    assert es_block["endpoint"] == "http://127.0.0.1:9200"
    assert es_block["index_pattern"] == "logs-*"
    assert es_block["service"] == "payment-api"
    assert es_block["hit_count"] == 1

    # The observations include at least one with source=elasticsearch.
    es_obs = [o for o in body["observations"] if o["source"] == "elasticsearch"]
    assert len(es_obs) >= 1
    assert es_obs[0]["kind"] == "generic_log_line"
    assert es_obs[0]["data"]["message"] == "OutOfMemoryError"
    # Service surfaced as part of the normalised data (Phase 1 V2 field
    # is masked by the API serializer, so we look at data).
    assert es_obs[0]["data"].get("service") == "payment-api"


# ---------------------------------------------------------------------------
# ES block absent: behaviour unchanged
# ---------------------------------------------------------------------------
def test_no_elasticsearch_block_preserves_phase1_behaviour(
    server, workspace
):
    repo = _create_repo(workspace / "no_es_repo")

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
        },
    )
    assert status == 201, body
    # elasticsearch payload key must NOT appear when no block was sent.
    assert "elasticsearch" not in body
    # No ES observations either.
    assert all(o["source"] != "elasticsearch" for o in body["observations"])


# ---------------------------------------------------------------------------
# Validation: bad URL scheme → 400
# ---------------------------------------------------------------------------
def test_unsupported_scheme_returns_400(server, workspace):
    repo = _create_repo(workspace / "bad_scheme_repo")
    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "elasticsearch": {
                "url": "ftp://es.example.com",
                "index_pattern": "logs-*",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 400
    assert "scheme" in body["error"].lower() or "http" in body["error"].lower()


# ---------------------------------------------------------------------------
# Validation: missing index_pattern → 400
# ---------------------------------------------------------------------------
def test_missing_index_pattern_returns_400(server, workspace):
    repo = _create_repo(workspace / "missing_idx_repo")
    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "elasticsearch": {
                "url": "http://127.0.0.1:9200",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 400
    assert "index_pattern" in body["error"].lower()


# ---------------------------------------------------------------------------
# Integration error surfaced as 400
# ---------------------------------------------------------------------------
def test_integration_failure_surfaces_as_400(server, workspace, monkeypatch):
    repo = _create_repo(workspace / "fail_repo")
    monkeypatch.setenv("AUTORCA_ES_ALLOW_LOOPBACK", "1")

    def fake_collect_with_metadata(self, ctx=None):
        raise IntegrationError("connection refused by mock")

    monkeypatch.setattr(
        ElasticsearchCollector,
        "collect_with_metadata",
        fake_collect_with_metadata,
    )

    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "elasticsearch": {
                "url": "http://127.0.0.1:9200",
                "index_pattern": "logs-*",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
            },
        },
    )
    assert status == 400
    assert "elasticsearch" in body["error"].lower()


# ---------------------------------------------------------------------------
# extra_headers may not override Authorization
# ---------------------------------------------------------------------------
def test_extra_headers_cannot_override_authorization(
    server, workspace, monkeypatch
):
    repo = _create_repo(workspace / "hdr_repo")
    monkeypatch.setenv("AUTORCA_ES_ALLOW_LOOPBACK", "1")
    status, body = _post(
        f"{server}/api/v1/investigations",
        {
            "repo": str(repo),
            "environment": "production",
            "elasticsearch": {
                "url": "http://127.0.0.1:9200",
                "index_pattern": "logs-*",
                "incident_start": "2026-08-27T10:00:00Z",
                "incident_end": "2026-08-27T11:00:00Z",
                "extra_headers": {"Authorization": "Bearer stolen"},
            },
        },
    )
    assert status == 400
    assert "authorization" in body["error"].lower()
