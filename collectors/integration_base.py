"""
collectors/integration_base.py
-----------------------------------------------------------------------------
Integration abstraction for AutoRCA Phase 2.1 + Phase 2.2.

Defines the minimum reusable contract that any external integration
collector (Elasticsearch, Prometheus, Kubernetes, GitHub, GitLab,
CI/CD) must satisfy. The contract is intentionally small — it borrows
heavily from `collectors/base.py` and adds only the few fields a
remote-system query truly needs.

Design rules:

- Source-agnostic. No integration-specific knowledge leaks here.
- Backward compatible. `IncidentContext` and `CollectedItem` come from
  `collectors/base.py` verbatim; nothing about Phase 0/Phase 1 changes.
- Loud failure. `IntegrationError` is raised by every collector for any
  unrecoverable failure (network, auth, schema, size, timeout).
- Bounded. `IntegrationConfig` enforces size, timeout, and time-window
  bounds at construction time. Collectors cannot bypass them.
- Composable. A future PrometheusCollector or GitHubCollector only
  needs to subclass / consume this; the API layer is already
  integration-shaped.

Phase 2.2 changes:

- `index_pattern` is now optional (Prometheus queries are typically a
  free-form PromQL string, not a fixed pattern; GitHub/GitLab do not
  use a pattern at all but a `repo` field). Validation is delegated to
  each collector's source-specific grammar.
- A new `resource` field captures the provider-neutral target
  (repository slug for GitHub/GitLab, namespace for K8s, …).
- A new `auth_scheme` field (`"basic"`, `"bearer"`, `"header"`)
  selects how the resolved secret is attached.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, NamedTuple, Optional

from collectors.base import CollectedItem, IncidentContext


logger = logging.getLogger(__name__)


# ---- Hard caps (defence in depth; collectors may also enforce their own) ----
MAX_RESULT_SIZE = 1000           # upper bound on `size`
MIN_RESULT_SIZE = 1
DEFAULT_RESULT_SIZE = 100
MAX_TIMEOUT_SECONDS = 30.0       # never let an HTTP call run forever
MIN_TIMEOUT_SECONDS = 0.5
DEFAULT_TIMEOUT_SECONDS = 5.0

# Maximum permitted time window for any single integration call (defence
# against accidentally pulling years of history).
MAX_WINDOW_SECONDS = 6 * 3600     # 6 hours

# Generic identifier grammar (used by GitHub / GitLab repo slugs):
# letters, digits, dash, underscore, dot, slash (org/repo), no whitespace.
_GENERIC_IDENT = re.compile(r"^[A-Za-z0-9._/-]+$")

# Prometheus metric name grammar (subset of the official spec):
# letters, digits, underscore, colon.
_METRIC_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")


class IntegrationError(RuntimeError):
    """Raised by any integration collector for any unrecoverable failure.

    Collectors MUST raise this — never return an empty result silently
    when the upstream failed. The investigation API converts this into
    an HTTP 400 so the caller knows the integration broke (and the
    pipeline was not polluted by a misleading empty result).
    """


AuthScheme = Literal["basic", "bearer", "header"]


@dataclass(frozen=True)
class IntegrationConfig:
    """Configuration for an external-system collector.

    `source` identifies the logical integration (`"elasticsearch"`,
    `"prometheus"`, `"github_changes"`, `"gitlab_changes"`, …).
    `endpoint` is the absolute URL of the remote service. The
    resource selector is split into:

    - `resource`  — provider-neutral target (GitHub/GitLab repo slug,
      K8s namespace, …). Validated by `validate_resource()`.
    - `index_pattern` — optional, used by Elasticsearch for an index
      pattern, by Prometheus for a metric name. If absent the
      collector falls back to a default appropriate for its source.

    Authentication is read from an environment variable name (`auth_env`)
    rather than a literal secret. `auth_scheme` selects how the
    resolved secret is attached to the HTTP request:

    - `"basic"`  → `Authorization: Basic <base64(secret)>` (default
      for backwards compatibility with Phase 2.1 Elasticsearch).
    - `"bearer"` → `Authorization: Bearer <secret>` (GitHub, GitLab).
    - `"header"` → `X-Api-Key: <secret>` (Prometheus with bearer token).

    All bounds are enforced at construction so a misconfigured caller
    cannot cause a runaway query.
    """

    source: str
    endpoint: str
    index_pattern: Optional[str] = None
    resource: Optional[str] = None
    query: Optional[str] = None
    service: Optional[str] = None
    incident_start: Optional[dt.datetime] = None
    incident_end: Optional[dt.datetime] = None
    size: int = DEFAULT_RESULT_SIZE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    auth_env: Optional[str] = None
    auth_scheme: AuthScheme = "basic"
    verify_tls: bool = True
    extra_headers: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source or not self.source.strip():
            raise ValueError("IntegrationConfig.source is required")
        if not self.endpoint or not self.endpoint.strip():
            raise ValueError("IntegrationConfig.endpoint is required")
        if self.resource is not None:
            if not self.resource.strip():
                raise ValueError("IntegrationConfig.resource must be non-empty")
            if not _GENERIC_IDENT.match(self.resource):
                raise ValueError(
                    f"IntegrationConfig.resource contains illegal "
                    f"characters: {self.resource!r}"
                )
        if self.index_pattern is not None and self.index_pattern.strip():
            # The collector decides whether this is an index pattern
            # (ES) or a metric name (Prometheus). We do not enforce a
            # specific grammar here.
            if "\x00" in self.index_pattern or "\n" in self.index_pattern:
                raise ValueError(
                    "IntegrationConfig.index_pattern contains illegal bytes"
                )
        if self.size < MIN_RESULT_SIZE:
            raise ValueError(
                f"IntegrationConfig.size must be >= {MIN_RESULT_SIZE}"
            )
        if self.size > MAX_RESULT_SIZE:
            # Clamp silently — collect() shouldn't blow up because the
            # caller asked for too much.
            object.__setattr__(self, "size", MAX_RESULT_SIZE)
        if self.timeout_seconds < MIN_TIMEOUT_SECONDS:
            raise ValueError(
                f"IntegrationConfig.timeout_seconds must be >= "
                f"{MIN_TIMEOUT_SECONDS}"
            )
        if self.timeout_seconds > MAX_TIMEOUT_SECONDS:
            object.__setattr__(self, "timeout_seconds", MAX_TIMEOUT_SECONDS)
        if self.service is not None and not self.service.strip():
            object.__setattr__(self, "service", None)
        if (
            self.incident_start is not None
            and self.incident_end is not None
        ):
            if self.incident_start > self.incident_end:
                raise ValueError(
                    "IntegrationConfig.incident_start must be <= incident_end"
                )
            delta = (self.incident_end - self.incident_start).total_seconds()
            if delta > MAX_WINDOW_SECONDS:
                raise ValueError(
                    f"IntegrationConfig time window exceeds the "
                    f"{MAX_WINDOW_SECONDS}s cap (got {delta:.0f}s)"
                )
        if self.auth_scheme not in ("basic", "bearer", "header"):
            raise ValueError(
                f"IntegrationConfig.auth_scheme must be one of "
                f"basic/bearer/header; got {self.auth_scheme!r}"
            )


class IntegrationResult(NamedTuple):
    """Carrier for the raw items a collector emits, plus diagnostic
    metadata that may be surfaced to the UI through
    `investigation.payload['integration']`.

    `items` is the canonical `CollectedItem` list (consumed by the
    extractor). `metadata` is the side-channel: how many hits came back,
    what index was queried, what time window was applied — never the
    raw provider responses themselves.
    """

    items: tuple[CollectedItem, ...]
    metadata: Dict[str, Any]


__all__ = [
    "IntegrationConfig",
    "IntegrationError",
    "IntegrationResult",
    "MAX_RESULT_SIZE",
    "MIN_RESULT_SIZE",
    "DEFAULT_RESULT_SIZE",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_WINDOW_SECONDS",
    "AuthScheme",
]
