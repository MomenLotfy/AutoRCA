from __future__ import annotations

import re
from typing import List

from extractors.base import BaseExtractor, ExtractionContext, Location, Observation
from extractors.registry import ExtractorMetadata, registry

_KEY_ERROR_PATTERN = re.compile(
    r"KeyError:\s*(?:['\"](?P<key_quoted>[A-Za-z_][A-Za-z0-9_]*)['\"]"
    r"|(?P<key_bare>[A-Za-z_][A-Za-z0-9_]*))"
)

_TRACEBACK_FRAME_PATTERN = re.compile(
    r'File\s+"(?P<file>[^"]+)",\s+line\s+(?P<line>\d+)'
)


@registry.register(
    ExtractorMetadata(
        extractor_id="missing_env_traceback_extractor",
        version="1.0.0",
        source="traceback",
        produces_kinds=("key_error",),
        description=(
            "يستخلص KeyError من نص الـ traceback مباشرة، ويحاول ربطه "
            "بأقرب إطار (file, line) سابق له في نفس الـ traceback."
        ),
    )
)
class MissingEnvTracebackExtractor(BaseExtractor):
    EXTRACTOR_ID = "missing_env_traceback_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw_content = context.raw_content

        if not raw_content or not raw_content.strip():
            return observations

        seen_keys: set[str] = set()

        for match in _KEY_ERROR_PATTERN.finditer(raw_content):
            key = match.group("key_quoted") or match.group("key_bare")
            if key is None or key in seen_keys:
                continue
            seen_keys.add(key)

            file_name, line_number = self._find_nearest_frame(raw_content, match.start())

            observations.append(
                self.build_observation(
                    context=context,
                    kind="key_error",
                    location=Location(file=file_name, line=line_number),
                    data={"key": key},
                    raw_reference=match.group(0),
                )
            )

        return observations

    @staticmethod
    def _find_nearest_frame(raw_content: str, match_start_index: int):
        preceding_text = raw_content[:match_start_index]
        frames = list(_TRACEBACK_FRAME_PATTERN.finditer(preceding_text))
        if not frames:
            return None, None

        last_frame = frames[-1]
        return last_frame.group("file"), int(last_frame.group("line"))
