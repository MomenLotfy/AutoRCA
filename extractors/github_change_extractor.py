"""
extractors/github_change_extractor.py
-----------------------------------------------------------------------------
Phase 2.2 — parse the JSON envelope emitted by
``GitHubChangeCollector`` (``source="github_changes"``) into
``Observation`` objects.

The extractor consumes the canonical envelope:

    {
      "type": "github_changes",
      "repo": "octocat/Hello-World",
      "service": "...",
      "incident_start": "...",
      "incident_end": "...",
      "event_count": ...,
      "events": [
        {"id": "...", "kind": "commit" | "pr",
         "title": "...", "author": "...",
         "timestamp": "...", "url": "...",
         "ref": "...", "sha": "...",
         "extra": {"merged": ..., "state": ..., ...}},
        ...
      ]
    }

One ``Observation`` per change event. Each ``Observation`` has
``kind="generic_log_line"`` (no schema bump) and ``source="github_changes"``
so it flows into the same deterministic pipeline as Phase 1
sources.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import List

from extractors.base import (
    BaseExtractor,
    ExtractionContext,
    Location,
    Observation,
)
from extractors.registry import ExtractorMetadata, registry


@registry.register(
    ExtractorMetadata(
        extractor_id="github_change_extractor",
        version="1.0.0",
        source="github_changes",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.2 — parses the JSON envelope emitted by "
            "GitHubChangeCollector into Observations of kind "
            "'generic_log_line' (source='github_changes')."
        ),
    )
)
class GitHubChangeExtractor(BaseExtractor):
    EXTRACTOR_ID = "github_change_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw = context.raw_content
        if not raw or not raw.strip():
            return observations

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return observations
        if not isinstance(payload, dict):
            return observations
        if payload.get("type") != "github_changes":
            return observations

        repo = payload.get("repo")
        envelope_service = payload.get("service")
        if not isinstance(envelope_service, str):
            envelope_service = None

        events = payload.get("events") or []
        if not isinstance(events, list):
            return observations

        for event in events:
            if not isinstance(event, dict):
                continue

            ts_value = event.get("timestamp")
            timestamp_known = True
            extracted_at = _iso_now()
            if isinstance(ts_value, str) and ts_value.strip():
                extracted_at = ts_value
            else:
                timestamp_known = False

            data: dict = {}
            kind = event.get("kind")
            if isinstance(kind, str) and kind:
                data["kind"] = kind
            title = event.get("title")
            if isinstance(title, str) and title:
                data["title"] = title[:256]
            author = event.get("author")
            if isinstance(author, str) and author:
                data["author"] = author
            sha = event.get("sha")
            if isinstance(sha, str) and sha:
                data["sha"] = sha
            ref = event.get("ref")
            if isinstance(ref, str) and ref:
                data["ref"] = ref
            url = event.get("url")
            if isinstance(url, str) and url:
                data["url"] = url
            extra = event.get("extra") or {}
            if isinstance(extra, dict):
                for key in ("merged", "state", "target_branch"):
                    if key in extra:
                        data[key] = extra[key]

            if not data:
                continue

            location = Location(
                step_name=f"github:{repo}" if repo else "github",
            )

            service_value = envelope_service or None

            observations.append(
                self.build_observation(
                    context=context,
                    kind="generic_log_line",
                    location=location,
                    data=data,
                    raw_reference=(
                        f"github:{repo}#{event.get('id', '')}"
                        if repo
                        else (raw[:256] if isinstance(raw, str) else "")
                    ),
                    extracted_at=extracted_at,
                    service=service_value,
                    resource=f"github:{repo}" if repo else None,
                    normalized_value=str(data.get("title") or "")[:120] or None,
                    timestamp_known=timestamp_known,
                )
            )

        return observations


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["GitHubChangeExtractor"]
