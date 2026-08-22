from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from extractors.registry import VALID_SOURCES, ExtractorMetadata, registry

VALID_OBSERVATION_KINDS: Tuple[str, ...] = (
    "key_error",
    "module_not_found_error",
    "address_in_use_error",
    "diff_removed_line",
    "diff_added_line",
    "exit_code_nonzero",
    "generic_log_line",
)


class ObservationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Location:
    file: Optional[str] = None
    line: Optional[int] = None
    commit_sha: Optional[str] = None
    step_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[object]]:
        return {
            "file": self.file,
            "line": self.line,
            "commit_sha": self.commit_sha,
            "step_name": self.step_name,
        }


@dataclass(frozen=True)
class Observation:
    id: str
    analysis_id: str
    schema_version: int
    kind: str
    source: str
    producer_id: str
    producer_version: str
    location: Location
    data: Dict[str, object]
    raw_reference: str
    extracted_at: str

    def __post_init__(self) -> None:
        if self.kind not in VALID_OBSERVATION_KINDS:
            raise ObservationValidationError(
                f"kind غير صالح '{self.kind}'. القيم المسموحة: {VALID_OBSERVATION_KINDS}"
            )
        if self.source not in VALID_SOURCES:
            raise ObservationValidationError(
                f"source غير صالح '{self.source}'. القيم المسموحة: {VALID_SOURCES}"
            )
        if not self.raw_reference or not self.raw_reference.strip():
            raise ObservationValidationError(
                "raw_reference مطلوب دائمًا في Observation."
            )
        if not self.data:
            raise ObservationValidationError(
                "data يجب ألا يكون فارغًا."
            )

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "analysis_id": self.analysis_id,
            "schema_version": self.schema_version,
            "kind": self.kind,
            "source": self.source,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "location": self.location.to_dict(),
            "data": self.data,
            "raw_reference": self.raw_reference,
            "extracted_at": self.extracted_at,
        }


class ObservationIdGenerator:
    def __init__(self, start: int = 1) -> None:
        if start < 1:
            raise ValueError("start يجب أن يكون 1 أو أكبر.")
        self._counter = start

    def next_id(self) -> str:
        current = self._counter
        self._counter += 1
        return f"O{current}"

    @property
    def issued_count(self) -> int:
        return self._counter - 1


@dataclass(frozen=True)
class ExtractionContext:
    """
    سياق عملية استخلاص واحدة (Extraction Run)، وليس سياق Extractor واحد
    بمفرده. id_generator مشترك بين كل الـ Extractors العاملة على نفس
    analysis_id لضمان عدم تصادم معرفات O1, O2, ...
    """

    analysis_id: str
    raw_content: str
    id_generator: ObservationIdGenerator


class BaseExtractor(abc.ABC):
    EXTRACTOR_ID: str = ""

    def get_metadata(self) -> ExtractorMetadata:
        if not self.EXTRACTOR_ID:
            raise NotImplementedError(f"{type(self).__name__} يجب أن يعرّف EXTRACTOR_ID.")
        return registry.get_metadata(self.EXTRACTOR_ID)

    @abc.abstractmethod
    def extract(self, context: ExtractionContext) -> List[Observation]:
        raise NotImplementedError

    def build_observation(
        self,
        *,
        context: ExtractionContext,
        kind: str,
        location: Location,
        data: Dict[str, object],
        raw_reference: str,
        extracted_at: Optional[str] = None,
    ) -> Observation:
        metadata = self.get_metadata()
        if kind not in metadata.produces_kinds:
            raise ObservationValidationError(
                f"Extractor '{metadata.extractor_id}' حاول إنتاج kind='{kind}' "
                f"خارج produces_kinds المُعلنة: {metadata.produces_kinds}"
            )

        return Observation(
            id=context.id_generator.next_id(),
            analysis_id=context.analysis_id,
            schema_version=1,
            kind=kind,
            source=metadata.source,
            producer_id=metadata.extractor_id,
            producer_version=metadata.version,
            location=location,
            data=data,
            raw_reference=raw_reference,
            extracted_at=extracted_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        )
