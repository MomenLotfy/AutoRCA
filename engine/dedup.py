"""
engine/dedup.py
-----------------------------------------------------------------------------
Phase 2.3 — safe deterministic deduplication of observations.

Multiple sources may report the same logical event:

    Prometheus memory signal
    + Docker memory signal
    + Kubernetes memory signal

The integration layer must NOT accidentally turn those into three
unrelated fake incidents. Equally, it must NOT collapse distinct events
into one.

Strategy:

- Each observation carries ``source`` (the upstream system) and a
  ``data`` dict (the projected payload).
- The dedup key is ``(source, kind, fingerprint_key)`` where
  ``fingerprint_key`` is a stable, content-derived key.
- Provenance is preserved: when two observations would otherwise be
  deduplicated (e.g. two observations from different sources reporting
  the same memory signal), we keep BOTH but mark them with a shared
  ``dedup_group`` id so downstream correlation engines can see they
  describe the same underlying signal.

Phase 2.3 only deduplicates observations that are LITERALLY identical
across the same source (e.g. Prometheus reported the same series twice
because two collectors were configured). Cross-source observations are
never silently collapsed; they are grouped but each survives.

This module deliberately does NOT modify the deterministic engine
semantics: it operates on the observation list returned by the
extractors, BEFORE the engine sees them. The engine still receives a
list of observations and decides what to do.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DedupOutcome:
    """Result of running deterministic dedup over an observation list.

    ``observations`` is the post-dedup list (preserves order, keeps
    first-seen). ``groups`` maps each post-dedup observation id to a
    shared group id (e.g. ``"group:abc123"``) when it shared a
    fingerprint with one or more other observations. ``dropped_count``
    is the number of literal-duplicate observations that were removed.
    """

    observations: List[object]
    groups: dict
    dropped_count: int


def _safe_getattr(obj, name, default=None):
    return getattr(obj, name, default)


def _fingerprint_data(data) -> str:
    """Build a stable hash of an observation's `data` dict.

    The hash is content-derived (not identity-derived). Two
    observations with identical `data` from the SAME source produce
    the same fingerprint; observations from different sources may
    share a fingerprint and will be grouped but not deduplicated.
    """
    if data is None:
        return ""
    try:
        # Sort keys for stable JSON. Use ensure_ascii=False so non-ASCII
        # keys (e.g. Arabic labels used elsewhere in the codebase) do
        # not produce different fingerprints on different platforms.
        import json

        canonical = json.dumps(
            data, sort_keys=True, ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):
        canonical = repr(data)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:16]


def _fingerprint_key(obs) -> Tuple[str, str, str]:
    """Compute (source, kind, content_hash) for an observation.

    `source` and `kind` come from the frozen Observation contract.
    `content_hash` is a stable hash of the observation's `data` dict
    plus its `resource` (which is the upstream identifier and part of
    provenance)."""
    source = str(_safe_getattr(obs, "source", "") or "")
    kind = str(_safe_getattr(obs, "kind", "") or "")
    resource = str(_safe_getattr(obs, "resource", "") or "")
    data = _safe_getattr(obs, "data", None)
    return (source, kind, _fingerprint_data({"data": data, "resource": resource}))


def _observation_id(obs, fallback_idx: int) -> str:
    obs_id = _safe_getattr(obs, "id", None)
    if isinstance(obs_id, str) and obs_id:
        return obs_id
    return f"OBS-AUTO-{fallback_idx:04d}"


def deduplicate_observations(
    observations: Sequence[object],
) -> DedupOutcome:
    """Deduplicate observations deterministically.

    Algorithm:

    1. Compute ``(source, kind, content_hash)`` for each observation.
    2. Within the SAME ``(source, kind)`` group, drop literal duplicates
       (observations whose content_hash is already in the kept set).
    3. Across DIFFERENT sources but identical ``(kind, content_hash)``,
       keep all observations but assign them a shared
       ``dedup_group`` id (via ``group`` map). The deterministic engine
       can use this to fuse cross-source signals later.

    Order is preserved: the first-seen observation for each
    ``(source, kind, content_hash)`` triple is the one that survives.

    Returns a ``DedupOutcome`` with the surviving observations, the
    group map, and a count of dropped literal duplicates.
    """
    kept: List[object] = []
    groups: dict = {}
    seen_within_source: dict = {}
    cross_source_groups: dict = {}
    dropped_count = 0
    next_group_idx = 0

    for idx, obs in enumerate(observations):
        key = _fingerprint_key(obs)
        if key in seen_within_source:
            dropped_count += 1
            logger.debug(
                "dedup: dropping literal duplicate obs idx=%d source=%s "
                "kind=%s",
                idx,
                key[0],
                key[1],
            )
            continue
        seen_within_source[key] = obs

        # Cross-source grouping: only meaningful for sources other than
        # the current observation's source AND same (kind, content_hash).
        content_key = (key[1], key[2])  # (kind, content_hash)
        if content_key in cross_source_groups:
            # Attach the existing group id; do NOT add to cross_source_groups.
            group_id = cross_source_groups[content_key]
        else:
            group_id = f"group:gr{next_group_idx:04d}"
            cross_source_groups[content_key] = group_id
            next_group_idx += 1
        groups[_observation_id(obs, idx)] = group_id
        kept.append(obs)

    return DedupOutcome(
        observations=kept,
        groups=groups,
        dropped_count=dropped_count,
    )


__all__ = ["DedupOutcome", "deduplicate_observations"]
