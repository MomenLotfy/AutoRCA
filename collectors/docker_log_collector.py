"""
collectors/docker_log_collector.py
-----------------------------------------------------------------------------
DockerLogCollector — wraps `docker logs` for a single container, returns
the verbatim text in the same shape as `FileCollector.collect`.

This collector is opt-in (gated by `AUTORCA_DOCKER_HOST` and the explicit
target container). It NEVER exposes arbitrary `docker exec` / shell
access — only the read-only `docker logs` command, with arguments
controlled as a fixed list (no shell=True).

The collector is designed to fail loudly if the Docker daemon is
unreachable; silent empty results are forbidden by the Collector contract.
"""
from __future__ import annotations

import os
import shlex
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


class DockerLogCollector(BaseCollector):
    """Collect `docker logs` output for a single container.

    Parameters
    ----------
    container : str
        Container name or ID. Validated to be a non-empty token (no
        whitespace, no path separators — `docker` itself rejects
        malformed values, but we pre-validate to avoid noisy errors).
    docker_host : Optional[str]
        Docker daemon socket URI. If None, reads `AUTORCA_DOCKER_HOST`
        from the environment. If still unset, the collector reports
        itself unavailable.
    since : Optional[str]
        Optional `since` filter passed to `docker logs`. ISO-8601 or
        relative duration (e.g. "10m") — both supported by docker.
    until : Optional[str]
        Optional `until` filter passed to `docker logs`.
    timestamps : bool
        If True, requests `docker logs --timestamps` so the output
        carries ISO timestamps in front of each line. These timestamps
        are preserved verbatim and used by IncidentContext filtering.
    tail : Optional[str]
        Optional `tail` argument to limit log volume (e.g. "1000").
    """

    name = "docker_log"

    def __init__(
        self,
        container: str,
        *,
        docker_host: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        timestamps: bool = True,
        tail: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not container or not container.strip():
            raise CollectorError("DockerLogCollector requires a container name.")
        if any(ch.isspace() for ch in container) or "/" in container or ".." in container:
            raise CollectorError(
                f"Invalid container name: {container!r}"
            )
        self._container = container.strip()
        self._docker_host = docker_host or os.environ.get("AUTORCA_DOCKER_HOST")
        self._since = since
        self._until = until
        self._timestamps = timestamps
        self._tail = tail
        self._timeout = float(timeout_seconds)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        if not self._docker_host:
            return False
        if not self._docker_binary():
            return False
        return True

    @staticmethod
    def _docker_binary() -> Optional[str]:
        for candidate in (os.environ.get("AUTORCA_DOCKER_BIN"), "docker"):
            if not candidate:
                continue
            if Path(candidate).exists() or _which(candidate):
                return candidate
        return None

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    def collect(self, ctx: Optional[IncidentContext] = None) -> List[CollectedItem]:
        docker_bin = self._docker_binary()
        if docker_bin is None:
            raise CollectorError(
                "DockerLogCollector: 'docker' binary not found in PATH."
            )

        args: List[str] = [docker_bin]
        if self._docker_host and self._docker_host != "unix:///var/run/docker.sock":
            args.extend(["-H", self._docker_host])
        args += ["logs"]
        if self._timestamps:
            args.append("--timestamps")
        if self._since:
            args.extend(["--since", self._since])
        if self._until:
            args.extend(["--until", self._until])
        if self._tail:
            args.extend(["--tail", self._tail])
        args.append(self._container)

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise CollectorError(f"docker binary missing: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CollectorError(
                f"docker logs timed out after {self._timeout}s"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise CollectorError(f"docker logs failed: {exc}") from exc

        # `docker logs` returns 0 even if the container has never written
        # anything; non-zero means a real failure (e.g. unknown container).
        if result.returncode != 0:
            raise CollectorError(
                f"docker logs for {self._container!r} failed "
                f"(exit={result.returncode}): {result.stderr.strip()}"
            )

        raw = result.stdout or ""
        if not raw.strip():
            # Not an error — empty logs are valid evidence of "nothing happened".
            return [
                CollectedItem(
                    source="docker_output",
                    raw_text="",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    metadata={"container": self._container, "lines": 0},
                )
            ]

        # Honour IncidentContext by filtering timestamps when --timestamps was on.
        if ctx is not None and ctx.is_set() and self._timestamps:
            raw = _filter_log_by_window(raw, ctx)

        return [
            CollectedItem(
                source="docker_output",
                raw_text=raw,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "container": self._container,
                    "lines": raw.count("\n") + 1 if raw else 0,
                },
            )
        ]


def _which(name: str) -> Optional[str]:
    """Minimal `which` — searches PATH for an executable file."""
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


def _filter_log_by_window(raw: str, ctx: IncidentContext) -> str:
    """Filter `docker logs --timestamps` output to IncidentContext window.

    Lines without parseable timestamps are kept (we never fabricate)
    only if the window is fully unbounded.
    """
    fully_bounded = ctx.incident_start is not None and ctx.incident_end is not None
    if fully_bounded:
        # We must filter; drop lines without parseable timestamps too
        # because we cannot prove they belong in the window.
        keep_unknown = False
    else:
        keep_unknown = True

    out_lines: List[str] = []
    for line in raw.splitlines():
        ts = _extract_line_timestamp(line)
        if ts is None:
            if keep_unknown:
                out_lines.append(line)
            continue
        if ctx.contains(ts):
            out_lines.append(line)
    return "\n".join(out_lines)


def _extract_line_timestamp(line: str) -> Optional[datetime]:
    """Extract the leading ISO timestamp from a `docker logs --timestamps` line."""
    if not line:
        return None
    # docker prints e.g. "2024-08-23T13:55:01.123456789Z err ..."
    head = line[:40]
    space_idx = head.find(" ")
    if space_idx <= 0:
        return None
    return parse_iso_timestamp(head[:space_idx])


__all__ = ["DockerLogCollector"]


# Silence unused-import warning for shlex (kept for future shell-safe arg quoting).
_ = shlex