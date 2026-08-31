"""
extractors/host_metrics_extractor.py
-----------------------------------------------------------------------------
host_metrics_extractor — يستخرج قيود المضيف من JSON الذي ينتجه
HostMetricsCollector (source=host_metrics).

كل عنصر في الـ JSON list (snapshot واحد) يصبح Observation من نوع
`host_metrics` مع المقاييس الطبيعية في `data` (mem_total_bytes,
mem_available_bytes, mem_percent, cpu_percent, load1, load5, load15,
disk_max_percent, disk_filesystems).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import List

from extractors.base import (
    VALID_OBSERVATION_KINDS,
    BaseExtractor,
    ExtractionContext,
    Location,
    Observation,
    ObservationValidationError,
)
from extractors.registry import ExtractorMetadata, registry

if "host_metrics" not in VALID_OBSERVATION_KINDS:
    raise ObservationValidationError(
        "VALID_OBSERVATION_KINDS لا يحتوي 'host_metrics'."
    )


@registry.register(
    ExtractorMetadata(
        extractor_id="host_metrics_extractor",
        version="1.0.0",
        source="host_metrics",
        produces_kinds=("host_metrics",),
        description=(
            "يستخرج قيود المضيف (memory, CPU, load, disk pressure) من "
            "JSON المنظم الذي ينتجه HostMetricsCollector."
        ),
    )
)
class HostMetricsExtractor(BaseExtractor):
    EXTRACTOR_ID = "host_metrics_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw = context.raw_content
        if not raw or not raw.strip():
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
            extracted_at = _iso_now()
            timestamp_known = True
            ts_value = record.get("timestamp")
            if isinstance(ts_value, str) and ts_value.strip():
                extracted_at = ts_value
            else:
                timestamp_known = False

            data: dict = {
                "mem_total_bytes": record.get("mem_total_bytes"),
                "mem_available_bytes": record.get("mem_available_bytes"),
                "mem_used_bytes": record.get("mem_used_bytes"),
                "mem_percent": record.get("mem_percent"),
                "cpu_percent": record.get("cpu_percent"),
                "load1": record.get("load1"),
                "load5": record.get("load5"),
                "load15": record.get("load15"),
                "disk_max_percent": record.get("disk_max_percent"),
            }
            if isinstance(record.get("disk_filesystems"), list):
                data["disk_filesystems"] = record["disk_filesystems"]
            data = {k: v for k, v in data.items() if v is not None}

            observations.append(
                self.build_observation(
                    context=context,
                    kind="host_metrics",
                    location=Location(),
                    data=data,
                    raw_reference=str(record)[:512],
                    extracted_at=extracted_at,
                    service="host",
                    resource="host",
                    normalized_value=record.get("mem_percent"),
                    timestamp_known=timestamp_known,
                )
            )

        return observations


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["HostMetricsExtractor"]
