"""
extractors/docker_event_extractor.py
-----------------------------------------------------------------------------
docker_event_extractor — يستخرج أحداث Docker من نص JSON الذي ينتجه
DockerEventCollector (source=docker_events).

كل عنصر في الـ JSON list يصبح Observation من نوع `container_event`
مع البيانات المنظمة في `data` (event, container, timestamp, actor).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import List

from collectors.base import parse_iso_timestamp
from collectors.docker_event_collector import is_container_event_payload
from extractors.base import (
    VALID_OBSERVATION_KINDS,
    BaseExtractor,
    ExtractionContext,
    Location,
    Observation,
    ObservationValidationError,
)
from extractors.registry import ExtractorMetadata, registry

_VALID_EVENT_KINDS = ("container_event",)
if "container_event" not in VALID_OBSERVATION_KINDS:
    raise ObservationValidationError(
        "VALID_OBSERVATION_KINDS لا يحتوي 'container_event'."
    )


@registry.register(
    ExtractorMetadata(
        extractor_id="docker_event_extractor",
        version="1.0.0",
        source="docker_events",
        produces_kinds=("container_event",),
        description=(
            "يستخرج أحداث Docker (create/start/stop/die/restart/kill/oom/"
            "health_status/destroy) من JSON الذي ينتجه DockerEventCollector."
        ),
    )
)
class DockerEventExtractor(BaseExtractor):
    EXTRACTOR_ID = "docker_event_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw = context.raw_content
        if not raw or not raw.strip():
            return observations
        if not is_container_event_payload(raw):
            return observations

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return observations

        if not isinstance(payload, list):
            return observations

        for record in payload:
            if not isinstance(record, dict):
                continue
            event_name = record.get("event")
            container = record.get("container") or ""
            ts = record.get("timestamp")
            actor = record.get("actor") if isinstance(record.get("actor"), dict) else {}
            extracted_at = _iso_now()
            timestamp_known = True
            if isinstance(ts, str) and ts.strip():
                parsed = parse_iso_timestamp(ts)
                if parsed is not None:
                    extracted_at = parsed.isoformat()
                else:
                    timestamp_known = False
                    extracted_at = _iso_now()
            else:
                timestamp_known = False
                extracted_at = _iso_now()

            data: dict = {
                "event": str(event_name or "unknown"),
                "container": str(container),
                "actor": dict(actor),
            }
            if isinstance(ts, str):
                data["raw_timestamp"] = ts

            observations.append(
                self.build_observation(
                    context=context,
                    kind="container_event",
                    location=Location(),
                    data=data,
                    raw_reference=str(record)[:512],
                    extracted_at=extracted_at,
                    service=str(container) if container else None,
                    resource=f"container:{container}" if container else None,
                    normalized_value=event_name,
                    timestamp_known=timestamp_known,
                )
            )

        return observations


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["DockerEventExtractor"]
