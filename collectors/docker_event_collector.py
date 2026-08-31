"""
collectors/docker_event_collector.py
-----------------------------------------------------------------------------
DockerEventCollector — wraps `docker events` for a container (or all
containers) and normalises lifecycle events into Observations consumable
by the deterministic RCA engine.

Events supported:
  create, start, stop, die, restart, kill, oom, health_status, destroy

Phase 1 strategy:
  - Run `docker events --format '{{json .}}' --since <ts> --until <ts>`
    with a fixed argument list (no shell).
  - One Observation per event with `kind = "container_event"` and a
    structured `data` dict carrying `event`, `container`, `timestamp`,
    `actor`, `attributes`.
  - Observations flow through the existing pipeline as `source =
    "docker_output"` so the existing docker_output extractors can pick
    up any incidental stack traces from container_event payloads
    (rare; this source is event metadata, not log lines).

This collector is OPT-IN. It is never invoked unless an explicit
container / host configuration is provided.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from collectors.base import (
    BaseCollector,
    CollectedItem,
    CollectorError,
    IncidentContext,
    parse_iso_timestamp,
)


SUPPORTED_EVENTS: tuple[str, ...] = (
    "create",
    "start",
    "stop",
    "die",
    "restart",
    "kill",
    "oom",
    "health_status",
    "destroy",
)


class DockerEventCollector(BaseCollector):
    """Collect `docker events` JSON output and produce one CollectedItem per event.

    Parameters
    ----------
    container : Optional[str]
        Restrict to a single container. None means "all containers".
    docker_host : Optional[str]
        Docker daemon URI (default: env AUTORCA_DOCKER_HOST).
    since, until : Optional[str]
        Time-window filters. If both are None and `ctx` is provided, the
        IncidentContext window is used.
    """

    name = "docker_event"

    def __init__(
        self,
        container: Optional[str] = None,
        *,
        docker_host: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if container is not None and (
            not container.strip()
            or any(ch.isspace() for ch in container)
            or "/" in container
        ):
            raise CollectorError(f"Invalid container name: {container!r}")
        self._container = container.strip() if container else None
        self._docker_host = docker_host or os.environ.get("AUTORCA_DOCKER_HOST")
        self._since = since
        self._until = until
        self._timeout = float(timeout_seconds)

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        if not self._docker_host:
            return False
        if not _which("docker"):
            return False
        return True

    # ------------------------------------------------------------------
    def collect(self, ctx: Optional[IncidentContext] = None) -> List[CollectedItem]:
        docker_bin = _which("docker")
        if docker_bin is None:
            raise CollectorError("DockerEventCollector: 'docker' binary not found.")

        # Resolve time window: explicit args > IncidentContext > unbounded.
        since = self._since
        until = self._until
        if ctx is not None and ctx.is_set():
            since = since or _iso(ctx.incident_start)
            until = until or _iso(ctx.incident_end)

        args: List[str] = [docker_bin]
        if self._docker_host and self._docker_host != "unix:///var/run/docker.sock":
            args.extend(["-H", self._docker_host])
        args += ["events", "--format", "{{json .}}"]
        if since:
            args += ["--since", since]
        if until:
            args += ["--until", until]
        if self._container:
            args += ["--filter", f"container={self._container}"]

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CollectorError(
                f"docker events timed out after {self._timeout}s"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise CollectorError(f"docker events failed: {exc}") from exc

        if result.returncode != 0:
            raise CollectorError(
                f"docker events failed (exit={result.returncode}): "
                f"{result.stderr.strip()}"
            )

        events: List[dict] = []
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)

        # Build a JSON document that mirrors the per-event Observation
        # payload (one CollectedItem with the whole document as raw_text
        # — the new docker_event extractor parses it). We deliberately
        # do NOT use the existing docker_output extractors for events;
        # events are structured, not log lines.
        if not events:
            return [
                CollectedItem(
                    source="docker_events",
                    raw_text="[]",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    metadata={"event_count": 0},
                )
            ]

        lines_out: List[str] = []
        for ev in events:
            event_name = (ev.get("status") or ev.get("Action") or "").lower()
            if event_name and event_name not in SUPPORTED_EVENTS:
                # Keep unknown events but tag them so downstream can decide.
                pass
            actor = ev.get("Actor") or {}
            attrs = actor.get("Attributes") if isinstance(actor, dict) else {}
            ts = ev.get("time") or ev.get("Time") or ev.get("timestamp")
            container = (
                (actor.get("Attributes") or {}).get("name")
                if isinstance(actor, dict)
                else None
            ) or ev.get("id") or self._container or ""
            record = {
                "type": "container_event",
                "event": event_name or "unknown",
                "container": container,
                "timestamp": ts,
                "actor": attrs if isinstance(attrs, dict) else {},
            }
            lines_out.append(json.dumps(record, ensure_ascii=False))

        body = "[\n" + ",\n".join(lines_out) + "\n]"
        return [
            CollectedItem(
                source="docker_events",
                raw_text=body,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={"event_count": len(events)},
            )
        ]


def _which(name: str) -> Optional[str]:
    if os.sep in name:
        return name if os.access(name, os.X_OK) else None
    path = os.environ.get("PATH", "")
    for entry in path.split(os.pathsep):
        if not entry:
            continue
        candidate = os.path.join(entry, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # docker --since / --until accept RFC 3339 (which is a subset of ISO-8601).
    return value.astimezone(timezone.utc).isoformat()


# Public hook used by the new event extractor (Phase 1.2).
_CONTAINER_EVENT_LINE = re.compile(r"\s*\{\s*\"type\"\s*:\s*\"container_event\"")


def is_container_event_payload(raw: str) -> bool:
    """Cheap pre-check used by extractors to decide whether a buffer
    is a docker_events payload (JSON array) vs a log/diff."""
    if not raw or "container_event" not in raw:
        return False
    return _CONTAINER_EVENT_LINE.search(raw) is not None or raw.lstrip().startswith("[")


__all__ = ["DockerEventCollector", "is_container_event_payload", "SUPPORTED_EVENTS"]