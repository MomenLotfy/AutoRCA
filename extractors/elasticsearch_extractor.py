"""
extractors/elasticsearch_extractor.py
-----------------------------------------------------------------------------
Phase 2.1 — parse the JSON envelope emitted by
``ElasticsearchCollector`` (``source="elasticsearch"``) into
``Observation`` objects.

The extractor consumes the canonical envelope:

    {
      "type": "elasticsearch_search",
      "index_pattern": "...",
      "service": "...",
      "incident_start": "...",
      "incident_end": "...",
      "size": ...,
      "hit_count": ...,
      "status": ...,
      "hits": [
        {"index": "...", "id": "...", "timestamp": "...",
         "message": "...", "service": "...", "level": "...", "host": "..."},
        ...
      ]
    }

One ``Observation`` per hit, all with ``kind="generic_log_line"`` so
they integrate with the existing deterministic pipeline (no new
schema kind, no schema bump). The Observation's ``data`` dict carries
the safe whitelisted fields plus the index/document id for
traceability. ``service`` flows into ``Observation.service`` so the
Phase-1 V2 evidence model can correlate ES hits with Docker metrics.

No ``engine/`` logic is referenced here.
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
        extractor_id="elasticsearch_extractor",
        version="1.0.0",
        source="elasticsearch",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.1 — parses the JSON envelope emitted by "
            "ElasticsearchCollector into Observations of kind "
            "'generic_log_line' (source='elasticsearch')."
        ),
    )
)
class ElasticsearchExtractor(BaseExtractor):
    EXTRACTOR_ID = "elasticsearch_extractor"

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

        if payload.get("type") != "elasticsearch_search":
            return observations

        hits = payload.get("hits") or []
        if not isinstance(hits, list):
            return observations

        index_pattern = payload.get("index_pattern")
        if not isinstance(index_pattern, str):
            index_pattern = None

        envelope_service = payload.get("service")
        if not isinstance(envelope_service, str):
            envelope_service = None

        incident_start = payload.get("incident_start")
        if not isinstance(incident_start, str):
            incident_start = None
        incident_end = payload.get("incident_end")
        if not isinstance(incident_end, str):
            incident_end = None

        for record in hits:
            if not isinstance(record, dict):
                continue

            ts_value = record.get("timestamp")
            timestamp_known = True
            extracted_at = _iso_now()
            if isinstance(ts_value, str) and ts_value.strip():
                extracted_at = ts_value
            else:
                timestamp_known = False

            data: dict = {}
            for key in ("message", "level", "host", "service"):
                value = record.get(key)
                if isinstance(value, str) and value:
                    data[key] = value

            doc_index = record.get("index")
            if isinstance(doc_index, str) and doc_index:
                data["es_index"] = doc_index

            doc_id = record.get("id")
            if isinstance(doc_id, str) and doc_id:
                data["es_doc_id"] = doc_id

            if not data:
                # ES hit produced nothing safe to report — skip rather
                # than fabricate.
                continue

            service_value = data.get("service") or envelope_service or None

            location = Location(
                step_name=f"elasticsearch:{index_pattern}" if index_pattern else "elasticsearch",
            )

            observations.append(
                self.build_observation(
                    context=context,
                    kind="generic_log_line",
                    location=location,
                    data=data,
                    raw_reference=(
                        f"{doc_index}/{doc_id}"
                        if (doc_index and doc_id)
                        else (raw[:256] if isinstance(raw, str) else "")
                    ),
                    extracted_at=extracted_at,
                    service=service_value,
                    resource=f"elasticsearch:{index_pattern}" if index_pattern else None,
                    normalized_value=data.get("level") or data.get("message", "")[:120] or None,
                    timestamp_known=timestamp_known,
                )
            )

        return observations


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["ElasticsearchExtractor"]
