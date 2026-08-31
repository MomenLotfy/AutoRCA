"""
collectors/base.py
-----------------------------------------------------------------------------
Collector protocol — the single seam where new incident sources plug in.

A Collector is the producer side of the {source_name: raw_text} contract that
feeds the deterministic pipeline. Every collector, including the existing
GitCollector and FileCollector, must satisfy this Protocol without changes
to their public method names — the adapter layer below absorbs the shape
difference so the pipeline always sees `Dict[str, str]`.

Phase 1 extensions live as siblings in this package:
    - DockerLogCollector         (docker logs <container>)
    - DockerEventCollector       (docker events --since/--until)
    - DockerMetricsCollector     (docker stats --no-stream)
    - HostMetricsCollector       (/proc, /sys, df — optional)
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Protocol, runtime_checkable


class CollectorError(RuntimeError):
    """Raised by any collector for any unrecoverable I/O / parse failure."""


@dataclass(frozen=True)
class IncidentContext:
    """
    Optional window that all collectors MAY honour.

    `incident_start` / `incident_end` are inclusive timestamps (UTC).
    When set, collectors should restrict collection to the window
    `[incident_start, incident_end]`. When either bound is None, that
    side of the window is unbounded.

    `service` and `deployment` are advisory hints — collectors decide
    whether they apply.

    All fields are optional; defaulting to None keeps the dataclass
    fully backward compatible with Phase 0 callers that construct a
    `PipelineInput` with no context.
    """

    incident_start: Optional[datetime] = None
    incident_end: Optional[datetime] = None
    service: Optional[str] = None
    deployment: Optional[str] = None

    def contains(self, ts: Optional[datetime]) -> bool:
        """Return True if `ts` is inside the window; None timestamps are
        only accepted if the window is unbounded on that side.

        This is the canonical filter used by collectors that can honour
        time windows. Collectors that cannot filter by time MUST return
        all observations and let the pipeline decide (we never fabricate
        timestamps).
        """
        if ts is None:
            # No timestamp available — accept only if window is unbounded.
            return self.incident_start is None and self.incident_end is None
        if self.incident_start is not None and ts < self.incident_start:
            return False
        if self.incident_end is not None and ts > self.incident_end:
            return False
        return True

    def is_set(self) -> bool:
        return (
            self.incident_start is not None
            or self.incident_end is not None
            or self.service is not None
            or self.deployment is not None
        )


@dataclass(frozen=True)
class CollectedItem:
    """
    Single item produced by a collector. Two fields only:

    - `source` — the logical source key used by `PipelineInput.sources`
      (e.g. "docker_output", "docker_events", "docker_metrics",
      "host_metrics").
    - `raw_text` — the verbatim text payload, exactly as the existing
      `FileCollector` would have returned. The deterministic extractors
      downstream parse this text; collectors do not interpret it.
    - `timestamp` — optional, ISO-8601 string. Never fabricated.
    - `metadata` — optional structured side-channel (e.g. metrics parsed
      out-of-band so extractors can short-circuit regex work). Not used
      by the existing pipeline; available for future evidence-V2 wiring.
    """

    source: str
    raw_text: str
    timestamp: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Collector(Protocol):
    """
    Minimal collector interface.

    `name` is a stable identifier (used in logs / metadata).
    `is_available()` reports whether the collector can run in the current
        environment (e.g. Docker socket reachable, /proc readable).
    `collect(ctx)` returns the raw items. Raises CollectorError on
        unrecoverable failure — collectors must NOT silently return []
        when they could not gather evidence; that would mask incidents.
    """

    name: str

    def is_available(self) -> bool: ...

    def collect(self, ctx: Optional[IncidentContext] = None) -> Iterable[CollectedItem]: ...


class BaseCollector(abc.ABC):
    """
    Convenience base class. Concrete collectors may extend this for a
    shared `name` field and a uniform `is_available()` default.
    """

    name: str = ""

    def is_available(self) -> bool:
        """Default: assume available. Concrete collectors override."""
        return True

    @abc.abstractmethod
    def collect(self, ctx: Optional[IncidentContext] = None) -> Iterable[CollectedItem]:
        raise NotImplementedError


def now_utc_iso() -> str:
    """UTC ISO-8601 with explicit timezone. Used for collector timestamps."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_timestamp(value: object) -> Optional[datetime]:
    """Best-effort ISO-8601 parser. Returns None on any failure rather
    than fabricating a value. Used by collectors that need to apply
    IncidentContext time-window filtering."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Tolerate trailing Z (UTC zulu) which fromisoformat rejects on 3.10.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None