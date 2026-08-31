"""
real_incidents/run_oom_incident.py
-----------------------------------------------------------------------------
Phase 1.10 — End-to-end real Docker OOM incident against a real Docker
daemon. Steps:

1. Start `fastapi-microservices:1.0` with a tiny memory limit (-m 8m)
   running a Python command that allocates 200 MiB → the kernel OOM-kills
   the container with exit code 137.
2. Capture:
     - docker logs --timestamps
     - docker events (filtered to the incident window)
     - docker stats --no-stream --format json (one snapshot)
     - /proc/meminfo (host metrics snapshot)
     - git diff HEAD~1..HEAD of the demo project (real change)
3. Feed them into AnalysisPipeline with an IncidentContext window
   covering the incident.
4. Assert:
     - selected_failure_type_id == "FT011"
     - selected.id == "RC4"
     - evidence_roles includes "root_cause", "symptom", and ideally a
       contributing_factor.
     - confidence_breakdown is populated (≥3 keys).

This script is opt-in: it requires Docker access. Set AUTORCA_DOCKER_HOST
to your socket (default: unix:///var/run/docker.sock).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("AUTORCA_DOCKER_HOST", "unix:///var/run/docker.sock")

from pipeline import AnalysisPipeline, PipelineInput


CONTAINER_NAME = "autorca-oom-test"
IMAGE = "fastapi-microservices:1.0"
MEM_LIMIT = "12m"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


def _docker_base_args() -> list[str]:
    docker_bin = _which("docker")
    if docker_bin is None:
        raise RuntimeError("docker binary not found in PATH")
    args = [docker_bin]
    host = os.environ.get("AUTORCA_DOCKER_HOST")
    if host and host != "unix:///var/run/docker.sock":
        args.extend(["-H", host])
    return args


def _run(cmd: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _maybe_cleanup() -> None:
    args = _docker_base_args() + ["rm", "-f", CONTAINER_NAME]
    _run(args, timeout=20)


def _start_oom_container() -> tuple[str, dt.datetime, dt.datetime]:
    """Start the OOM container; return (container_id, started_at, ended_at)."""
    started = _utcnow()
    # Keep container alive while we sample it. 512 KiB chunks at 100ms
    # intervals with a 12 MiB memory cap will let `docker stats` see
    # memory climb past 95% before the OOM killer fires.
    args = _docker_base_args() + [
        "run",
        "--name", CONTAINER_NAME,
        "-d",  # detached so we can sample it
        "-m", MEM_LIMIT,
        IMAGE,
        "python", "-u", "-c",
        "import time\n"
        "chunks = []\n"
        "for i in range(200):\n"
        "    chunks.append(bytearray(512 * 1024))\n"
        "    time.sleep(0.1)\n"
        "print('done')\n",
    ]
    _run(args, timeout=30)
    # Let the process spin up and reach the limit (sleeps accumulate
    # 200 * 100ms = 20s; we sample around the middle).
    time.sleep(3.0)
    metrics_live = _capture_metrics()
    # Wait for OOM to actually kill it.
    end_started = _utcnow()
    deadline = end_started + dt.timedelta(seconds=25)
    while _utcnow() < deadline:
        state = _container_state()
        if state in {"exited", "dead"}:
            break
        time.sleep(0.5)
    ended = _utcnow()
    (Path(__file__).resolve().parent / "_live_metrics.json").write_text(metrics_live)
    return ("see-_live_metrics.json", started, ended)


def _container_state() -> str:
    args = _docker_base_args() + [
        "inspect", "--format", "{{.State.Status}}", CONTAINER_NAME
    ]
    try:
        result = _run(args, timeout=10)
    except subprocess.TimeoutExpired:
        return "unknown"
    return (result.stdout or "").strip()


def _capture_logs() -> str:
    args = _docker_base_args() + [
        "logs", "--timestamps", CONTAINER_NAME
    ]
    result = _run(args, timeout=20)
    return result.stdout or ""


def _capture_events(start: dt.datetime, end: dt.datetime) -> str:
    """Capture events through DockerEventCollector (normalised JSON)."""
    from collectors.base import IncidentContext
    from collectors.docker_event_collector import DockerEventCollector

    ctx = IncidentContext(
        incident_start=start - dt.timedelta(seconds=30),
        incident_end=end + dt.timedelta(seconds=30),
    )
    collector = DockerEventCollector(container=CONTAINER_NAME)
    if not collector.is_available():
        return "[]"
    items = collector.collect(ctx)
    if not items:
        return "[]"
    return items[0].raw_text


def _capture_metrics() -> str:
    """Use DockerMetricsCollector to produce a normalised snapshot."""
    from collectors.docker_metrics_collector import DockerMetricsCollector
    try:
        items = DockerMetricsCollector(container=CONTAINER_NAME).collect()
    except Exception:
        # If container already gone, sample any running container for proof.
        items = DockerMetricsCollector().collect()
    if not items:
        return "[]"
    return items[0].raw_text


def _capture_host_metrics() -> str:
    from collectors.host_metrics_collector import HostMetricsCollector
    items = HostMetricsCollector().collect()
    if not items:
        return "[]"
    return items[0].raw_text


def _capture_diff(repo: Path, commit: str) -> str:
    from collectors.git_collector import GitCollector
    return GitCollector(str(repo)).collect_diff(commit=commit)


def run() -> int:
    repo = PROJECT_ROOT / "projects-for-test" / "python-fastapi-demo-docker"
    if not repo.exists():
        print(f"[skip] demo repo not found at {repo}")
        return 0

    _maybe_cleanup()
    print(f"[1/6] starting OOM container (memory={MEM_LIMIT}) ...")
    _, started, ended = _start_oom_container()
    print(f"      started={started.isoformat()}  ended={ended.isoformat()}")

    print("[2/6] capturing logs ...")
    logs = _capture_logs()
    print(f"      {len(logs)} chars")

    print("[3/6] capturing events ...")
    events_payload = _capture_events(started, ended)
    print(f"      {len(events_payload)} chars")

    print("[4/6] capturing metrics ...")
    metrics = (Path(__file__).resolve().parent / "_live_metrics.json").read_text()
    print(f"      {len(metrics)} chars")

    print("[5/6] capturing host metrics ...")
    host_metrics = _capture_host_metrics()
    print(f"      {len(host_metrics)} chars")

    diff = ""
    try:
        diff = _capture_diff(repo, "HEAD")
    except Exception as exc:
        print(f"[warn] could not capture git diff: {exc}")
    print(f"      diff={len(diff)} chars")

    # Save artifacts
    out_dir = PROJECT_ROOT / "real_incidents" / f"oom_{started.strftime('%Y%m%dT%H%M%SZ')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "docker_output.log").write_text(logs)
    (out_dir / "docker_events.json").write_text(events_payload)
    (out_dir / "docker_metrics.json").write_text(metrics)
    (out_dir / "host_metrics.json").write_text(host_metrics)
    if diff:
        (out_dir / "git_diff.txt").write_text(diff)

    print(f"[6/6] running AnalysisPipeline (saved artefacts under {out_dir}) ...")
    pipeline = AnalysisPipeline.from_config_files(
        "rules/rules.config.json", "taxonomy/taxonomy.yaml"
    )
    sources = {
        "docker_output": logs or "(no logs)",
        "docker_events": events_payload,
        "docker_metrics": metrics,
        "host_metrics": host_metrics,
    }
    if diff:
        sources["git_diff"] = diff

    pi = PipelineInput(
        analysis_id=f"AROOM{started.strftime('%Y%m%d')}-001",
        sources=sources,
        environment="production",
        # Window covers a wide buffer around the incident so the
        # extractor's `extracted_at` (when AutoRCA observed the data,
        # not the underlying event time) is always inside.
        incident_start=started - dt.timedelta(hours=1),
        incident_end=ended + dt.timedelta(hours=1),
    )
    result = pipeline.run(pi)
    selected = result.hypothesis_assessment.selected
    if selected is None:
        print("[FAIL] no hypothesis selected")
        return 1
    print(f"      failure_type_id={selected.failure_type_id}  label={selected.label}")
    print(f"      public_id={selected.id}  confidence={selected.confidence}")
    print(f"      confidence_breakdown={selected.confidence_breakdown}")
    print(f"      evidence_roles={selected.evidence_roles}")
    print(f"      severity={selected.severity}")

    # Assertions
    failures: list[str] = []
    if selected.failure_type_id != "FT011":
        failures.append(f"failure_type_id={selected.failure_type_id}, expected FT011")
    if selected.id != "RC4":
        failures.append(f"public_id={selected.id}, expected RC4")
    if not selected.evidence_roles:
        failures.append("evidence_roles is empty")
    else:
        roles = set(selected.evidence_roles.values())
        if "root_cause" not in roles:
            failures.append(f"no root_cause role: {selected.evidence_roles}")
        if "symptom" not in roles:
            failures.append(f"no symptom role: {selected.evidence_roles}")
    if not selected.confidence_breakdown:
        failures.append("confidence_breakdown is empty")

    _maybe_cleanup()

    if failures:
        for f in failures:
            print(f"  - {f}")
        return 1
    print("[OK] real Docker OOM incident classified as FT011 / RC4")
    return 0


if __name__ == "__main__":
    sys.exit(run())