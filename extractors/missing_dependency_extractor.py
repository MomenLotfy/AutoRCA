from __future__ import annotations

import re
from typing import List, Optional, Tuple

from extractors.base import BaseExtractor, ExtractionContext, Location, Observation
from extractors.registry import ExtractorMetadata, registry

_PYTHON_MODULE_NOT_FOUND_PATTERN = re.compile(
    r"(?:ModuleNotFoundError|ImportError):\s*No module named\s*"
    r"(?:['\"](?P<module_quoted>[A-Za-z0-9_.\-]+)['\"]"
    r"|(?P<module_bare>[A-Za-z0-9_.\-]+))"
)

_NODE_MODULE_NOT_FOUND_PATTERN = re.compile(
    r"Cannot find module\s+['\"](?P<module>[^'\"]+)['\"]"
)

_TRACEBACK_FRAME_PATTERN = re.compile(
    r'File\s+"(?P<file>[^"]+)",\s+line\s+(?P<line>\d+)'
)

_NODE_REQUIRE_STACK_FILE_PATTERN = re.compile(
    r"^\s*-\s+(?P<file>.+\.[jt]sx?)\s*$", re.MULTILINE
)


@registry.register(
    ExtractorMetadata(
        extractor_id="missing_dependency_traceback_extractor",
        version="1.0.0",
        source="traceback",
        produces_kinds=("module_not_found_error",),
        description=(
            "يستخلص ModuleNotFoundError/ImportError (بايثون) و "
            "Cannot find module (Node.js) من نص الـ traceback."
        ),
    )
)
class MissingDependencyTracebackExtractor(BaseExtractor):
    EXTRACTOR_ID = "missing_dependency_traceback_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw_content = context.raw_content

        if not raw_content or not raw_content.strip():
            return observations

        seen_modules: set[str] = set()

        for match in _PYTHON_MODULE_NOT_FOUND_PATTERN.finditer(raw_content):
            module_name = match.group("module_quoted") or match.group("module_bare")
            observations.extend(self._build_if_new(context, module_name, match, raw_content, seen_modules))

        for match in _NODE_MODULE_NOT_FOUND_PATTERN.finditer(raw_content):
            module_name = match.group("module")
            observations.extend(self._build_if_new(context, module_name, match, raw_content, seen_modules))

        return observations

    def _build_if_new(
        self,
        context: ExtractionContext,
        module_name: Optional[str],
        match: "re.Match[str]",
        raw_content: str,
        seen_modules: set[str],
    ) -> List[Observation]:
        if module_name is None or module_name in seen_modules:
            return []
        seen_modules.add(module_name)

        file_name, line_number = self._find_location(raw_content, match.start())

        return [
            self.build_observation(
                context=context,
                kind="module_not_found_error",
                location=Location(file=file_name, line=line_number),
                data={"module": module_name},
                raw_reference=match.group(0),
            )
        ]

    @staticmethod
    def _find_location(raw_content: str, match_start_index: int) -> Tuple[Optional[str], Optional[int]]:
        preceding_text = raw_content[:match_start_index]
        frames = list(_TRACEBACK_FRAME_PATTERN.finditer(preceding_text))
        if frames:
            last_frame = frames[-1]
            return last_frame.group("file"), int(last_frame.group("line"))

        following_text = raw_content[match_start_index:]
        require_stack_match = _NODE_REQUIRE_STACK_FILE_PATTERN.search(following_text)
        if require_stack_match:
            return require_stack_match.group("file"), None

        return None, None
