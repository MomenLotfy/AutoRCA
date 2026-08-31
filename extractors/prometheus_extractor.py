"""
extractors/prometheus_extractor.py
-----------------------------------------------------------------------------
Phase 2.2 — parse the JSON envelope emitted by
``PrometheusCollector`` (``source="prometheus"``) into ``Observation``
objects.

The extractor consumes the canonical envelope:

    {
      "type": "prometheus_query",
      "mode": "instant" | "range",
      "query": "...",
      "metric_name": "...",
      "service": "...",
      "resource": "...",
      "incident_start": "...",
      "incident_end": "...",
      "step_seconds": ...,
      "size": ...,
      "status": ...,
      "sample_count": ...,
      "series_count": ...,
      "series": [
        {"labels": {"__name__": "...", ...},
         "value": [ts, val] OR "samples": [[ts, val], ...]},
        ...
      ]
    }

One ``Observation`` per series. Each ``Observation`` has
``kind="generic_log_line"`` (no schema bump) and ``source="prometheus"``
so it flows into the same deterministic pipeline as Phase 1
sources. The ``data`` dict carries a small whitelist of labels and the
headline metric value (``normalized_value``). The full series is in
the raw envelope and may be re-parsed by downstream stages if needed.
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


_SAFE_LABEL_KEYS = {
    "__name__",
    "job",
    "instance",
    "service",
    "pod",
    "namespace",
    "container",
    "code",
    "method",
    "status",
    "endpoint",
}


@registry.register(
    ExtractorMetadata(
        extractor_id="prometheus_extractor",
        version="1.0.0",
        source="prometheus",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.2 — parses the JSON envelope emitted by "
            "PrometheusCollector into Observations of kind "
            "'generic_log_line' (source='prometheus')."
        ),
    )
)
class PrometheusExtractor(BaseExtractor):
    EXTRACTOR_ID = "prometheus_extractor"

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
        if payload.get("type") != "prometheus_query":
            return observations

        query = payload.get("query")
        metric_name = payload.get("metric_name")
        envelope_service = payload.get("service")
        if not isinstance(envelope_service, str):
            envelope_service = None
        resource = payload.get("resource")

        series = payload.get("series") or []
        if not isinstance(series, list):
            return observations

        for entry in series:
            if not isinstance(entry, dict):
                continue
            labels = entry.get("labels") or {}
            if not isinstance(labels, dict):
                labels = {}

            safe_labels: dict = {}
            for key in _SAFE_LABEL_KEYS:
                if key in labels and isinstance(labels[key], str):
                    safe_labels[key] = labels[key][:128]

            # Pick a representative sample: either the single value
            # for vector results or the latest sample for matrix.
            headline_ts = None
            headline_val = None
            value = entry.get("value")
            if isinstance(value, list) and len(value) >= 2:
                headline_ts = _safe_float(value[0])
                headline_val = _safe_float(value[1])
            samples = entry.get("samples") or []
            if headline_val is None and isinstance(samples, list) and samples:
                last = samples[-1]
                if isinstance(last, list) and len(last) >= 2:
                    headline_ts = _safe_float(last[0])
                    headline_val = _safe_float(last[1])

            if headline_val is None:
                continue

            timestamp_known = headline_ts is not None
            extracted_at = (
                _unix_to_iso(headline_ts)
                if headline_ts is not None
                else _iso_now()
            )

            data: dict = {}
            if safe_labels:
                data["labels"] = safe_labels
            if query:
                data["query"] = str(query)[:256]
            data["value"] = headline_val

            series_name = (
                safe_labels.get("__name__")
                or metric_name
                or "prometheus"
            )

            service_value = (
                safe_labels.get("service") or envelope_service or None
            )

            location = Location(
                step_name=f"prometheus:{series_name}",
            )

            observations.append(
                self.build_observation(
                    context=context,
                    kind="generic_log_line",
                    location=location,
                    data=data,
                    raw_reference=(
                        json.dumps(entry, ensure_ascii=False, sort_keys=True)[:256]
                    ),
                    extracted_at=extracted_at,
                    service=service_value,
                    resource=f"prometheus:{resource or series_name}",
                    normalized_value=str(headline_val),
                    timestamp_known=timestamp_known,
                )
            )

        return observations


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unix_to_iso(value: float) -> str:
    try:
        return dt.datetime.fromtimestamp(
            float(value), tz=dt.timezone.utc
        ).isoformat()
    except (ValueError, OSError):
        return _iso_now()


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["PrometheusExtractor"]
