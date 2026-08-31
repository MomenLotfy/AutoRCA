from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, List, Tuple, Type

if TYPE_CHECKING:
    from extractors.base import BaseExtractor

logger = logging.getLogger(__name__)

VALID_SOURCES: Tuple[str, ...] = (
    "traceback",
    "git_diff",
    "ci_log",
    "docker_output",
    "test_output",
    # Phase 1 additions — docker_events / docker_metrics / host_metrics.
    "docker_events",
    "docker_metrics",
    "host_metrics",
    # Phase 2.1 — external observability integrations.
    "elasticsearch",
    # Phase 2.2 — additional external integrations.
    "prometheus",
    "github_changes",
    "gitlab_changes",
    # Phase 2.3 — Kubernetes + CI/CD.
    "kubernetes",
    "github_actions",
    "gitlab_ci",
    "jenkins",
)

_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class ExtractorMetadata:
    extractor_id: str
    version: str
    source: str
    produces_kinds: Tuple[str, ...]
    description: str
    priority: int = field(default=100)

    def __post_init__(self) -> None:
        if self.source not in VALID_SOURCES:
            raise ValueError(
                f"source غير صالح '{self.source}' للـ Extractor '{self.extractor_id}'. "
                f"القيم المسموحة: {VALID_SOURCES}"
            )
        if not self.produces_kinds:
            raise ValueError(
                f"الـ Extractor '{self.extractor_id}' يجب أن يحدد produces_kinds واحدًا على الأقل."
            )
        if not _SEMVER_PATTERN.match(self.version):
            raise ValueError(
                f"version غير صالح '{self.version}' للـ Extractor '{self.extractor_id}'. "
                "يجب أن يتبع Semantic Versioning بصيغة X.Y.Z (مثال: 1.0.0)."
            )


class DuplicateExtractorError(ValueError):
    pass


class UnknownExtractorError(KeyError):
    pass


class ExtractorRegistry:
    def __init__(self) -> None:
        self._extractor_classes: Dict[str, Type["BaseExtractor"]] = {}
        self._metadata: Dict[str, ExtractorMetadata] = {}

    def register(
        self, metadata: ExtractorMetadata
    ) -> Callable[[Type["BaseExtractor"]], Type["BaseExtractor"]]:
        def decorator(extractor_cls: Type["BaseExtractor"]) -> Type["BaseExtractor"]:
            if metadata.extractor_id in self._extractor_classes:
                raise DuplicateExtractorError(
                    f"Extractor id '{metadata.extractor_id}' مسجل بالفعل بواسطة "
                    f"{self._extractor_classes[metadata.extractor_id].__name__}."
                )
            self._extractor_classes[metadata.extractor_id] = extractor_cls
            self._metadata[metadata.extractor_id] = metadata
            logger.debug(
                "Registered extractor '%s' v%s for source=%s priority=%d",
                metadata.extractor_id,
                metadata.version,
                metadata.source,
                metadata.priority,
            )
            return extractor_cls

        return decorator

    def get_extractor_classes_for_source(
        self, source: str
    ) -> List[Type["BaseExtractor"]]:
        if source not in VALID_SOURCES:
            raise ValueError(f"source غير صالح: '{source}'")

        matching_ids = [
            extractor_id
            for extractor_id, meta in self._metadata.items()
            if meta.source == source
        ]
        matching_ids.sort(key=lambda eid: self._metadata[eid].priority)
        return [self._extractor_classes[eid] for eid in matching_ids]

    def get_metadata(self, extractor_id: str) -> ExtractorMetadata:
        if extractor_id not in self._metadata:
            raise UnknownExtractorError(f"لا يوجد Extractor مسجل بمعرف '{extractor_id}'")
        return self._metadata[extractor_id]

    def all_metadata(self) -> List[ExtractorMetadata]:
        return list(self._metadata.values())

    def all_registered_sources(self) -> List[str]:
        return sorted({meta.source for meta in self._metadata.values()})

    def is_registered(self, extractor_id: str) -> bool:
        return extractor_id in self._metadata


registry = ExtractorRegistry()
