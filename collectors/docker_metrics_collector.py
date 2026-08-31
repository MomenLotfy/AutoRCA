"""
collectors/docker_metrics_collector.py
-----------------------------------------------------------------------------
DockerMetricsCollector — wraps `docker stats --no-stream --format json`
to gather CPU / memory / network / block IO / PID / state metrics for
a single container (or all containers).

The collector returns ONE CollectedItem per container. The `raw_text` is
a structured JSON document of normalised metrics that the new
`docker_metrics_extractor` parses deterministically.

We intentionally do NOT use shell-parsing of `docker stats --format
"{{.CPUPerc}} ..."`. The JSON format is more robust, version-stable,
and avoids locale-dependent decimal separators. `docker stats --no-stream`
exits after one sample, so it has predictable runtime.

Metrics collected (per Docker stats JSON):
  - CPUPerc      (string, e.g. "12.34%")    -> normalised_value: float
  - MemUsage / MemLimit (e.g. "100MiB / 1GiB") -> bytes + pct
  - NetRx / NetTx                               -> bytes
  - BlockRead / BlockWrite                      -> bytes
  - PIDs                                        -> int
  - RestartCount                                -> int (from `docker inspect`, not stats)
  - State (running / exited / paused / restarting)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from collectors.base import (
    BaseCollector,
    CollectedItem,
    CollectorError,
)


_SIZE_PATTERN = re.compile(r"([0-9]*\.?[0-9]+)\s*([KMGTP]?i?B?)", re.IGNORECASE)


class DockerMetricsCollector(BaseCollector):
    """Collect a single sample of metrics per container.

    Parameters
    ----------
    container : Optional[str]
        Restrict to one container. None means "all running containers".
    docker_host : Optional[str]
        Docker daemon URI (default: env AUTORCA_DOCKER_HOST).
    """

    name = "docker_metrics"

    def __init__(
        self,
        container: Optional[str] = None,
        *,
        docker_host: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if container is not None and (
            not container.strip()
            or any(ch.isspace() for ch in container)
        ):
            raise CollectorError(f"Invalid container name: {container!r}")
        self._container = container.strip() if container else None
        self._docker_host = docker_host or os.environ.get("AUTORCA_DOCKER_HOST")
        self._timeout = float(timeout_seconds)

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        if not self._docker_host:
            return False
        if not _which("docker"):
            return False
        return True

    # ------------------------------------------------------------------
    def collect(self, ctx: Optional[IncidentContext] = None) -> List[CollectedItem]:  # noqa: ARG002
        docker_bin = _which("docker")
        if docker_bin is None:
            raise CollectorError("DockerMetricsCollector: 'docker' binary not found.")

        args: List[str] = [docker_bin]
        if self._docker_host and self._docker_host != "unix:///var/run/docker.sock":
            args.extend(["-H", self._docker_host])
        args += ["stats", "--no-stream", "--format", "json"]
        if self._container:
            args.append(self._container)

        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=self._timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise CollectorError(
                f"docker stats timed out after {self._timeout}s"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise CollectorError(f"docker stats failed: {exc}") from exc

        if result.returncode != 0:
            raise CollectorError(
                f"docker stats failed (exit={result.returncode}): "
                f"{result.stderr.strip()}"
            )

        containers: List[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                containers.append(obj)

        if not containers:
            return [
                CollectedItem(
                    source="docker_metrics",
                    raw_text="[]",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    metadata={"container_count": 0},
                )
            ]

        # Enrich with RestartCount + State from `docker inspect` (single call).
        try:
            inspect_args = [docker_bin]
            if self._docker_host and self._docker_host != "unix:///var/run/docker.sock":
                inspect_args.extend(["-H", self._docker_host])
            inspect_args += ["inspect"]
            if self._container:
                inspect_args.append(self._container)
            else:
                inspect_args.append("--format")
                inspect_args.append("{{json .}}")
            # NB: the default for "all containers" is per-id inspect;
            # we keep it simple and inspect only the target container
            # when one is specified, otherwise we skip inspect (state is
            # included in stats JSON).
            if self._container:
                inspect = subprocess.run(
                    inspect_args, capture_output=True, text=True, timeout=self._timeout
                )
                if inspect.returncode == 0:
                    obj = json.loads(inspect.stdout.strip())
                    if isinstance(obj, list) and obj:
                        first = obj[0]
                        restart_count = (
                            (first.get("RestartCount") if isinstance(first, dict) else None)
                            or 0
                        )
                        state_obj = (first.get("State") or {}) if isinstance(first, dict) else {}
                        state = state_obj.get("Status", "unknown") if isinstance(state_obj, dict) else "unknown"
                        for c in containers:
                            if c.get("Name") in (self._container, f"/{self._container}"):
                                c["_RestartCount"] = restart_count
                                c["_State"] = state
        except (subprocess.SubprocessError, json.JSONDecodeError):
            # Best-effort enrichment. Stats data is still valid.
            pass

        # Normalise into one JSON envelope per container for the extractor.
        normalised = [_normalise(c) for c in containers]
        body = json.dumps(normalised, ensure_ascii=False)
        return [
            CollectedItem(
                source="docker_metrics",
                raw_text=body,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={"container_count": len(normalised)},
            )
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _parse_size_to_bytes(value: str) -> Optional[int]:
    """Parse Docker's human-readable sizes (e.g. "100MiB", "1.5GiB", "0B")."""
    if not isinstance(value, str) or not value.strip():
        return None
    match = _SIZE_PATTERN.search(value)
    if not match:
        # Plain integer bytes
        try:
            return int(value.strip())
        except ValueError:
            return None
    number = float(match.group(1))
    unit = match.group(2).upper()
    factors = {
        "": 1, "B": 1,
        "K": 1024, "KB": 1024, "KIB": 1024,
        "M": 1024**2, "MB": 1024**2, "MIB": 1024**2,
        "G": 1024**3, "GB": 1024**3, "GIB": 1024**3,
        "T": 1024**4, "TB": 1024**4, "TIB": 1024**4,
        "P": 1024**5, "PB": 1024**5, "PIB": 1024**5,
    }
    return int(number * factors.get(unit, 1))


def _parse_percent(value: str) -> Optional[float]:
    if not isinstance(value, str):
        return None
    s = value.strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _normalise(container: dict) -> dict:
    """Produce a normalised metric record from one `docker stats` JSON entry."""
    name = container.get("Name") or container.get("Container") or ""
    container_id = container.get("ID") or container.get("ContainerID") or ""
    cpu_perc = _parse_percent(container.get("CPUPerc", ""))
    mem_usage_str, _, mem_limit_str = (container.get("MemUsage") or "").partition(" / ")
    mem_usage = _parse_size_to_bytes(mem_usage_str) if mem_usage_str else None
    mem_limit = _parse_size_to_bytes(mem_limit_str) if mem_limit_str else None
    mem_perc = _parse_percent(container.get("MemPerc", ""))
    if mem_perc is None and mem_usage is not None and mem_limit:
        mem_perc = round(100.0 * mem_usage / mem_limit, 2)
    net_rx_str, _, net_tx_str = (container.get("NetIO") or "").partition(" / ")
    net_rx = _parse_size_to_bytes(net_rx_str) if net_rx_str else None
    net_tx = _parse_size_to_bytes(net_tx_str) if net_tx_str else None
    block_read_str, _, block_write_str = (container.get("BlockIO") or "").partition(" / ")
    block_read = _parse_size_to_bytes(block_read_str) if block_read_str else None
    block_write = _parse_size_to_bytes(block_write_str) if block_write_str else None
    try:
        pids = int(container.get("PIDs", "0") or 0)
    except ValueError:
        pids = 0
    restart_count = container.get("_RestartCount")
    state = container.get("_State") or container.get("Status") or "unknown"
    return {
        "type": "container_metrics",
        "container": name.lstrip("/") if isinstance(name, str) else name,
        "container_id": container_id[:12] if container_id else "",
        "cpu_percent": cpu_perc,
        "mem_usage_bytes": mem_usage,
        "mem_limit_bytes": mem_limit,
        "mem_percent": mem_perc,
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
        "block_read_bytes": block_read,
        "block_write_bytes": block_write,
        "pids": pids,
        "restart_count": restart_count,
        "state": state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["DockerMetricsCollector"]