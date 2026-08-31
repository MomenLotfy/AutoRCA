"""
extractors/gitlab_change_extractor.py
-----------------------------------------------------------------------------
Phase 2.2 — parse the JSON envelope emitted by
``GitLabChangeCollector`` (``source="gitlab_changes"``) into
``Observation`` objects.

Mirror of ``github_change_extractor.py`` for GitLab. The envelope
shape is identical (defined in ``change_provider_base.py`` via
``ChangeEvent``); only ``source`` and ``resource`` naming differ.
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
        extractor_id="gitlab_change_extractor",
        version="1.0.0",
        source="gitlab_changes",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.2 — parses the JSON envelope emitted by "
            "GitLabChangeCollector into Observations of kind "
            "'generic_log_line' (source='gitlab_changes')."
        ),
    )
)
class GitLabChangeExtractor(BaseExtractor):
    EXTRACTOR_ID = "gitlab_change_extractor"

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
        if payload.get("type") != "gitlab_changes":
            return observations

        project = payload.get("project")
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
                step_name=f"gitlab:{project}" if project else "gitlab",
            )

            service_value = envelope_service or None

            observations.append(
                self.build_observation(
                    context=context,
                    kind="generic_log_line",
                    location=location,
                    data=data,
                    raw_reference=(
                        f"gitlab:{project}#{event.get('id', '')}"
                        if project
                        else (raw[:256] if isinstance(raw, str) else "")
                    ),
                    extracted_at=extracted_at,
                    service=service_value,
                    resource=f"gitlab:{project}" if project else None,
                    normalized_value=str(data.get("title") or "")[:120] or None,
                    timestamp_known=timestamp_known,
                )
            )

        return observations


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["GitLabChangeExtractor"]
