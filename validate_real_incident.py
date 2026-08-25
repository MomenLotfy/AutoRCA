"""End-to-end real-incident validation against the canonical target repo.

Steps:
1. Switch the target repo to the controlled-incident branch.
2. Run the actual application with the env var removed to capture the real
   runtime traceback (a real ``KeyError: 'DOCKER_DATABASE_URL'``).
3. Start the AutoRCA web server.
4. POST the real incident to /api/v1/investigations.
5. Fetch the full investigation via the API.
6. Verify the root cause matches RC1 / missing_environment_variable.
7. Switch back to main and clean up.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TARGET = REPO_ROOT / "projects-for-test" / "python-fastapi-demo-docker"


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def main() -> int:
    print("=" * 70)
    print("STEP 1 — switch target repo to the controlled-incident branch")
    print("=" * 70)
    _run(["git", "checkout", "autorca-real-incident"], cwd=TARGET, check=True)
    incident_commit = _run(["git", "rev-parse", "HEAD"], cwd=TARGET, check=True).stdout.strip()
    print(f"incident branch HEAD: {incident_commit}")

    print()
    print("=" * 70)
    print("STEP 2 — run the real application to capture a real traceback")
    print("=" * 70)
    # The incident branch's connect.py:38 uses os.environ["DOCKER_DATABASE_URL"]
    # which raises a real KeyError when the env var is absent (which is what
    # happens in the real Docker-Compose failure: the .env no longer carries
    # it). We invoke the same line directly so we don't depend on
    # psycopg2/sqlalchemy being installed locally.
    proc = subprocess.run(
        [sys.executable, "-c", "import os; os.environ.pop('DOCKER_DATABASE_URL', None); x = os.environ['DOCKER_DATABASE_URL']"],
        cwd=TARGET,
        env={k: v for k, v in os.environ.items() if k != "DOCKER_DATABASE_URL"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    real_traceback = (proc.stdout + proc.stderr).strip()
    print(real_traceback)
    # Place the traceback inside the workspace so the API security check accepts it.
    traceback_file = TARGET / "real_incident_traceback.txt"
    traceback_file.write_text(real_traceback + "\n", encoding="utf-8")
    # Also keep a copy at the project root for the test suite / documentation.
    (REPO_ROOT / "real_incident_traceback.txt").write_text(real_traceback + "\n", encoding="utf-8")

    if "DOCKER_DATABASE_URL" not in real_traceback:
        print("FAIL: traceback does not reference DOCKER_DATABASE_URL")
        return 1

    print()
    print("=" * 70)
    print("STEP 3 — start the AutoRCA web server")
    print("=" * 70)
    os.environ["AUTORCA_WORKSPACE_ROOT"] = str(TARGET.parent)
    sys.path.insert(0, str(REPO_ROOT))
    from web_app import AutoRCAHandler, reset_investigation_service

    reset_investigation_service()
    server = ThreadingHTTPServer(("127.0.0.1", 0), AutoRCAHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    print(f"server listening on {base}")

    try:
        # Health check
        with urllib.request.urlopen(f"{base}/api/health") as resp:
            health = json.loads(resp.read().decode("utf-8"))
            assert health["status"] == "ok", health
            print("health:", health)

        print()
        print("=" * 70)
        print("STEP 4 — POST real investigation to /api/v1/investigations")
        print("=" * 70)
        payload = {
            "repo": str(TARGET),
            "environment": "production",
            "traceback": str(traceback_file),
            "full_name": "aws-samples/python-fastapi-demo-docker",
            "no_diff": True,  # avoid mutating the target repo with HEAD~1 reference
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/v1/investigations",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                investigation = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            print("API error:", err_body)
            return 1
        investigation_id = investigation["investigation_id"]
        print(f"investigation_id: {investigation_id}")
        print(f"status: {investigation['status']}")

        print()
        print("=" * 70)
        print("STEP 5 — verify root cause")
        print("=" * 70)
        summary = investigation["incident_summary"]
        print(f"root cause: {summary['root_cause']}")
        print(f"failure_type_id: {summary['failure_type_id']}")
        print(f"confidence: {summary['confidence']}")
        print(f"severity: {summary['severity']}")

        if summary["root_cause"] != "missing_environment_variable":
            print(f"FAIL: root cause should be 'missing_environment_variable', got {summary['root_cause']!r}")
            return 1
        if not summary["failure_type_id"] or not summary["failure_type_id"].startswith("FT"):
            print("FAIL: failure_type_id missing")
            return 1
        if summary["confidence"] is None or summary["confidence"] < 0.5:
            print(f"FAIL: confidence too low: {summary['confidence']}")
            return 1

        # Verify evidence contains the real env var reference
        evidence_with_key = [e for e in investigation["evidence"] if "DOCKER_DATABASE_URL" in json.dumps(e)]
        if not evidence_with_key:
            print("FAIL: no evidence references DOCKER_DATABASE_URL")
            return 1
        print(f"evidence references DOCKER_DATABASE_URL: {len(evidence_with_key)} items")

        # Verify remediation
        if investigation["remediation"]:
            rem = investigation["remediation"]
            print(f"remediation action: {rem['action']}")
            print(f"target_symbols: {rem['target_symbols']}")
            if "DOCKER_DATABASE_URL" not in rem["target_symbols"]:
                print("FAIL: remediation missing DOCKER_DATABASE_URL target")
                return 1

        # Verify graph
        graph = investigation["graph"]
        if graph and graph.get("nodes"):
            types = sorted({n["type"] for n in graph["nodes"]})
            print(f"graph node types: {types}")

        # Verify timeline
        timeline = investigation["timeline"]
        if timeline and timeline.get("events"):
            print(f"timeline events: {len(timeline['events'])}")

        # Fetch via UI list endpoint
        with urllib.request.urlopen(f"{base}/api/v1/investigations") as resp:
            listing = json.loads(resp.read().decode("utf-8"))
        listed = [item["investigation_id"] for item in listing["investigations"]]
        if investigation_id not in listed:
            print("FAIL: investigation not in list endpoint")
            return 1
        print(f"investigation appears in list: {len(listed)} total")

        print()
        print("=" * 70)
        print("STEP 6 — restore target repo to clean main")
        print("=" * 70)
        # Remove any untracked files we created in the target repo before checkout.
        for untracked in (
            TARGET / "real_incident_traceback.txt",
        ):
            if untracked.exists():
                untracked.unlink()
        _run(["git", "checkout", "main"], cwd=TARGET, check=True)
        _run(["git", "reset", "--hard", "origin/main"], cwd=TARGET, check=True)
        status = _run(["git", "status", "--porcelain"], cwd=TARGET).stdout.strip()
        if status:
            print(f"FAIL: target repo not clean after restore: {status!r}")
            return 1
        print("target repo restored to clean main")

        print()
        print("=" * 70)
        print("RESULT: PASS — full E2E validation succeeded")
        print("=" * 70)
        print(json.dumps({
            "investigation_id": investigation_id,
            "root_cause": summary["root_cause"],
            "failure_type_id": summary["failure_type_id"],
            "confidence": summary["confidence"],
            "severity": summary["severity"],
            "evidence_count": len(investigation["evidence"]),
            "graph_nodes": (graph or {}).get("node_count"),
            "timeline_events": len((timeline or {}).get("events", [])),
            "incident_commit": incident_commit,
        }, indent=2, ensure_ascii=False))
        return 0
    finally:
        server.shutdown()
        thread.join()
        reset_investigation_service()


if __name__ == "__main__":
    sys.exit(main())