from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from web_app import AutoRCAHandler


@pytest.fixture()
def web_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), AutoRCAHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def test_ui_serves_real_analysis_form(web_server):
    with urllib.request.urlopen(f"{web_server}/") as response:
        body = response.read().decode("utf-8")
    assert response.status == 200
    assert "REAL INCIDENT INPUT" in body
    assert "/api/analyze" in urllib.request.urlopen(f"{web_server}/app.js").read().decode("utf-8")


def test_ui_api_fails_fast_for_missing_real_repository(web_server):
    request = urllib.request.Request(
        f"{web_server}/api/analyze",
        data=json.dumps({}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400
    assert "real Git repository" in error.value.read().decode("utf-8")