"""
engine/timeline_engine.py
-----------------------------------------------------------------------------
IncidentTimeline — تمثيل زمني منظم للحدث.

الفلسفة:
- كل TimelineEvent يحمل timestamp حقيقي إن وُجد، أو None صراحة إن لم
  يتوفر وقت محدد من المصدر. لا نصنع تواريخًا أبدًا.
- نوع الحدث (event_type) يُشتق من Observation/Evidence حقيقي فقط، وليس
  من حدس.
- الـ ordering يُحترم لو كانت التواريخ موجودة، وإلا الترتيب يتحدد
  بترتيب الإدراج في الـ pipeline (الذي يتبع ترتيب المصادر في sources).
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from extractors.base import Observation


VALID_EVENT_TYPES: tuple[str, ...] = (
    "git_commit",
    "configuration_change",
    "container_start",
    "container_failure",
    "application_startup",
    "traceback",
    "error_observation",
    "health_check_failure",
    "analysis_start",
)


class TimelineEngineError(ValueError):
    pass


@dataclass(frozen=True)
class TimelineEvent:
    id: str
    event_type: str
    timestamp: Optional[str]  # ISO-8601 or None (explicitly unknown)
    timestamp_known: bool    # False ⇒ لا يوجد وقت متاح من المصدر
    source: str
    description: str
    related_observation_ids: List[str] = field(default_factory=list)
    related_evidence_ids: List[str] = field(default_factory=list)
    related_change: Optional[str] = None  # commit SHA إن وُجد
    raw_reference: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "timestamp_known": self.timestamp_known,
            "source": self.source,
            "description": self.description,
            "related_observation_ids": list(self.related_observation_ids),
            "related_evidence_ids": list(self.related_evidence_ids),
            "related_change": self.related_change,
            "raw_reference": self.raw_reference,
        }


class TimelineIdGenerator:
    def __init__(self, start: int = 1) -> None:
        if start < 1:
            raise ValueError("start يجب أن يكون 1 أو أكبر.")
        self._counter = start

    def next_id(self) -> str:
        current = self._counter
        self._counter += 1
        return f"T{current}"


@dataclass(frozen=True)
class IncidentTimeline:
    analysis_id: str
    events: List[TimelineEvent]
    has_unknown_timestamps: bool
    earliest_known_timestamp: Optional[str]
    latest_known_timestamp: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "events": [e.to_dict() for e in self.events],
            "has_unknown_timestamps": self.has_unknown_timestamps,
            "earliest_known_timestamp": self.earliest_known_timestamp,
            "latest_known_timestamp": self.latest_known_timestamp,
        }


_KIND_TO_EVENT_TYPE: Dict[str, str] = {
    "key_error": "traceback",
    "module_not_found_error": "traceback",
    "address_in_use_error": "container_failure",
    "diff_removed_line": "configuration_change",
    "diff_added_line": "configuration_change",
    "exit_code_nonzero": "container_failure",
    "generic_log_line": "application_startup",
}


def _build_event_description(observation: Observation) -> str:
    kind = observation.kind
    data = observation.data
    if kind == "key_error" and "key" in data:
        return f"KeyError detected for variable '{data['key']}'"
    if kind == "module_not_found_error" and "module" in data:
        return f"Module not found: '{data['module']}'"
    if kind == "address_in_use_error":
        port = data.get("port")
        if port is not None:
            return f"Port conflict detected on port {port}"
        return "Address-already-in-use error detected"
    if kind in ("diff_removed_line", "diff_added_line") and "line_content" in data:
        prefix = "Removed" if kind == "diff_removed_line" else "Added"
        return f"{prefix} line in diff: {data['line_content'].strip()[:120]}"
    return f"Observation of kind '{kind}'"


class TimelineEngine:
    """
    يبني IncidentTimeline من:
    - قائمة Observations (من extraction).
    - commit_sha + extracted_at المُقترنة بكل observation.
    - analysis_started_at ISO-8601 اختياري، يُستخدم كحدث "analysis_start".

    لا يستدعي git ولا يخمن تواريخ. إذا observation بدون timestamp
    (المفروض أن يكون extracted_at موجودًا دائمًا) نضع timestamp=None
    ونرفع timestamp_known=False بصراحة.
    """

    def __init__(self) -> None:
        pass

    def build(
        self,
        *,
        analysis_id: str,
        observations: List[Observation],
        analysis_started_at: Optional[str] = None,
    ) -> IncidentTimeline:
        id_generator = TimelineIdGenerator()
        events: List[TimelineEvent] = []

        # event: analysis_start — يُضاف أولًا إذا توفر وقت بدء التحليل
        if analysis_started_at:
            events.append(
                TimelineEvent(
                    id=id_generator.next_id(),
                    event_type="analysis_start",
                    timestamp=analysis_started_at,
                    timestamp_known=True,
                    source="system",
                    description="AutoRCA analysis started",
                    related_observation_ids=[],
                    related_evidence_ids=[],
                    related_change=None,
                    raw_reference=None,
                )
            )

        for observation in observations:
            event_type = _KIND_TO_EVENT_TYPE.get(observation.kind, "error_observation")
            description = _build_event_description(observation)
            commit_sha = observation.location.commit_sha

            events.append(
                TimelineEvent(
                    id=id_generator.next_id(),
                    event_type=event_type,
                    timestamp=observation.extracted_at,
                    timestamp_known=observation.extracted_at is not None,
                    source=observation.source,
                    description=description,
                    related_observation_ids=[observation.id],
                    related_evidence_ids=[],
                    related_change=commit_sha,
                    raw_reference=observation.raw_reference,
                )
            )

        # حساب earliest/latest من التواريخ المعروفة فقط
        known_timestamps = [e.timestamp for e in events if e.timestamp_known and e.timestamp]
        earliest = min(known_timestamps) if known_timestamps else None
        latest = max(known_timestamps) if known_timestamps else None
        has_unknown = any(not e.timestamp_known for e in events)

        return IncidentTimeline(
            analysis_id=analysis_id,
            events=events,
            has_unknown_timestamps=has_unknown,
            earliest_known_timestamp=earliest,
            latest_known_timestamp=latest,
        )
