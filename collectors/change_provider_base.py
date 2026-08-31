"""
collectors/change_provider_base.py
-----------------------------------------------------------------------------
ChangeProvider — provider-neutral abstraction over code-hosting APIs
(GitHub, GitLab, Bitbucket, …).

A ``ChangeProvider`` exposes a small, frozen contract:

    collect_changes(repo, since, until, ctx) -> ChangeEvent list

The deterministic RCA engine does not care whether the upstream is
GitHub, GitLab, or any other source. It receives a stream of change
events normalised into the same envelope:

    {
      "id":           provider-specific id (str)
      "kind":         "commit" | "pr" | "merge_request" | "deployment"
      "title":        short human description
      "author":       login/username of the actor (may be None)
      "timestamp":    ISO-8601 UTC timestamp
      "url":          web URL of the change (may be None)
      "ref":          branch/tag name (commits only)
      "sha":          commit SHA (commits only)
      "extra":        source-specific dict (small whitelist)
    }

Concrete providers (GitHubCollector, GitLabCollector) implement
``ChangeProvider``. They are interchangeable; the API layer can
dispatch to either without knowing the underlying transport.

Auth, secrets, and URL validation are delegated to the underlying
``BaseCollector`` implementations — this module only deals with the
provider-neutral shape.

Phase 2.2 scope: the abstract protocol and two concrete impls
(GitHub, GitLab). Other hosts (Bitbucket, Gitea, Phabricator) can be
added later by implementing this protocol.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from collectors.base import IncidentContext


logger = logging.getLogger(__name__)


# Hard caps shared by every change provider.
MAX_CHANGES_PER_WINDOW = 200          # how many change events per call
MAX_WINDOW_DAYS = 7                   # range queries wider than this are capped
MIN_WINDOW_SECONDS = 60               # < 1 minute windows are not useful


@dataclass(frozen=True)
class ChangeEvent:
    """A provider-neutral change event.

    `kind` is one of ``commit``, ``pr``, ``merge_request``, ``deployment``.
    `extra` is a source-specific dict that has been projected through a
    small per-provider whitelist; never echo raw upstream payloads.
    """

    id: str
    kind: str
    title: str
    author: Optional[str]
    timestamp: Optional[str]
    url: Optional[str] = None
    ref: Optional[str] = None
    sha: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "author": self.author,
            "timestamp": self.timestamp,
            "url": self.url,
            "ref": self.ref,
            "sha": self.sha,
            "extra": dict(self.extra),
        }


@runtime_checkable
class ChangeProvider(Protocol):
    """Provider-neutral protocol every code-hosting collector implements.

    Concrete classes additionally extend ``BaseCollector`` from
    ``collectors/base.py`` so they can be plugged into the API layer's
    integration pattern. ``collect_changes`` is the only method that
    matters here; the rest of the BaseCollector surface
    (``is_available`` / ``collect`` / ``name``) is unchanged.
    """

    name: str

    def collect_changes(
        self,
        repo: str,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
        ctx: Optional[IncidentContext] = None,
    ) -> List[ChangeEvent]:
        """Return a bounded list of change events for the given repo
        in the given time window. The provider is responsible for
        bounding the result count itself."""

    def is_available(self) -> bool: ...


# ---------------------------------------------------------------------------
# Helpers shared by GitHub / GitLab
# ---------------------------------------------------------------------------
def clamp_window(
    since: Optional[dt.datetime],
    until: Optional[dt.datetime],
) -> tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    """Clamp a time window to the provider limits. Returns the
    (possibly adjusted) (since, until) pair. Either bound may be None
    to mean "open-ended", but at least one must be provided."""
    if since is None and until is None:
        raise ValueError(
            "ChangeProvider requires at least one of (since, until)"
        )
    if since is not None and until is not None:
        if since > until:
            raise ValueError(
                f"ChangeProvider window is reversed: since={since} "
                f"> until={until}"
            )
        delta_days = (until - since).total_seconds() / 86400.0
        if delta_days > MAX_WINDOW_DAYS:
            new_until = since + dt.timedelta(days=MAX_WINDOW_DAYS)
            logger.info(
                "ChangeProvider window clamped from %.1fd to %dd",
                delta_days,
                MAX_WINDOW_DAYS,
            )
            until = new_until
    if since is not None and until is not None:
        delta_s = (until - since).total_seconds()
        if delta_s < MIN_WINDOW_SECONDS:
            until = since + dt.timedelta(seconds=MIN_WINDOW_SECONDS)
    return since, until


def project_extra(extra: Dict[str, Any]) -> Dict[str, Any]:
    """Project a small whitelist of fields from a provider's extra
    metadata. Concrete providers decide which fields to keep."""
    out: Dict[str, Any] = {}
    for key in ("merged", "state", "labels", "target_branch", "deploy_env"):
        if key in extra:
            value = extra[key]
            if isinstance(value, (str, int, float, bool)):
                out[key] = value
            elif isinstance(value, list) and all(
                isinstance(v, str) for v in value
            ):
                out[key] = [v[:64] for v in value[:10]]
    return out


__all__ = [
    "ChangeEvent",
    "ChangeProvider",
    "MAX_CHANGES_PER_WINDOW",
    "MAX_WINDOW_DAYS",
    "MIN_WINDOW_SECONDS",
    "clamp_window",
    "project_extra",
]
