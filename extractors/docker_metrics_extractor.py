"""
extractors/docker_metrics_extractor.py
-----------------------------------------------------------------------------
docker_metrics_extractor — يستخرج قيود الحاوية من JSON الذي ينتجه
DockerMetricsCollector (source=docker_metrics).

كل عنصر في الـ JSON list (واحد لكل container) يصبح Observation من نوع
`container_metrics` مع كل المقاييس الطبيعية في `data` (cpu_percent,
mem_usage_bytes, mem_limit_bytes, mem_percent, net_rx_bytes, net_tx_bytes,
block_read_bytes, block_write_bytes, pids, restart_count, state).
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

if "container_metrics" not in VALID_OBSERVATION_KINDS:
    raise ObservationValidationError(
        "VALID_OBSERVATION_KINDS لا يحتوي 'container_metrics'."
    )


@registry.register(
    ExtractorMetadata(
        extractor_id="docker_metrics_extractor",
        version="1.0.0",
        source="docker_metrics",
        produces_kinds=("container_metrics",),
        description=(
            "يستخرج قيود الحاوية (CPU, memory, network, block IO, PIDs, "
            "restart count, state) من JSON المنظم الذي ينتجه "
            "DockerMetricsCollector."
        ),
    )
)
class DockerMetricsExtractor(BaseExtractor):
    EXTRACTOR_ID = "docker_metrics_extractor"

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
            container = str(record.get("container") or "")
            extracted_at = _iso_now()
            timestamp_known = True
            ts_value = record.get("timestamp")
            if isinstance(ts_value, str) and ts_value.strip():
                extracted_at = ts_value
            else:
                timestamp_known = False

            data: dict = {
                "cpu_percent": record.get("cpu_percent"),
                "mem_usage_bytes": record.get("mem_usage_bytes"),
                "mem_limit_bytes": record.get("mem_limit_bytes"),
                "mem_percent": record.get("mem_percent"),
                "net_rx_bytes": record.get("net_rx_bytes"),
                "net_tx_bytes": record.get("net_tx_bytes"),
                "block_read_bytes": record.get("block_read_bytes"),
                "block_write_bytes": record.get("block_write_bytes"),
                "pids": record.get("pids"),
                "restart_count": record.get("restart_count"),
                "state": record.get("state"),
            }
            data = {k: v for k, v in data.items() if v is not None}

            observations.append(
                self.build_observation(
                    context=context,
                    kind="container_metrics",
                    location=Location(),
                    data=data,
                    raw_reference=str(record)[:512],
                    extracted_at=extracted_at,
                    service=container or None,
                    resource=f"container:{container}" if container else None,
                    normalized_value=record.get("mem_percent"),
                    timestamp_known=timestamp_known,
                )
            )

        return observations


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["DockerMetricsExtractor"]
