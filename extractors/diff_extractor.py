from __future__ import annotations

import re
from typing import List, Optional

from extractors.base import BaseExtractor, ExtractionContext, Location, Observation
from extractors.registry import ExtractorMetadata, registry

_FILE_HEADER_PATTERN = re.compile(r"^diff --git a/(?P<a_path>\S+) b/(?P<b_path>\S+)")
_HUNK_HEADER_PATTERN = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,\d+)?\s+\+(?P<new_start>\d+)(?:,\d+)?\s+@@"
)


@registry.register(
    ExtractorMetadata(
        extractor_id="diff_line_extractor",
        version="1.0.0",
        source="git_diff",
        produces_kinds=("diff_removed_line", "diff_added_line"),
        description=(
            "يحلل unified diff سطرًا بسطر وينتج Observation لكل سطر مضاف "
            "أو محذوف، مع تتبع اسم الملف ورقم السطر الحاليين."
        ),
    )
)
class DiffLineExtractor(BaseExtractor):
    EXTRACTOR_ID = "diff_line_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw_content = context.raw_content

        if not raw_content or not raw_content.strip():
            return observations

        current_file: Optional[str] = None
        old_line_no: Optional[int] = None
        new_line_no: Optional[int] = None
        commit_sha = self._extract_commit_sha(raw_content)

        for raw_line in raw_content.splitlines():
            file_match = _FILE_HEADER_PATTERN.match(raw_line)
            if file_match:
                current_file = file_match.group("b_path")
                old_line_no = None
                new_line_no = None
                continue

            hunk_match = _HUNK_HEADER_PATTERN.match(raw_line)
            if hunk_match:
                old_line_no = int(hunk_match.group("old_start"))
                new_line_no = int(hunk_match.group("new_start"))
                continue

            if raw_line.startswith("\\"):
                # سطر تعليقي قياسي مثل "\ No newline at end of file".
                # لا يمثل سطرًا فعليًا في الملف، فلا يجوز أن يزيد أي عداد.
                continue

            if old_line_no is None or new_line_no is None:
                continue

            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                content = raw_line[1:]
                observations.append(
                    self.build_observation(
                        context=context,
                        kind="diff_added_line",
                        location=Location(file=current_file, line=new_line_no, commit_sha=commit_sha),
                        data={"line_content": content},
                        raw_reference=raw_line,
                    )
                )
                new_line_no += 1

            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                content = raw_line[1:]
                observations.append(
                    self.build_observation(
                        context=context,
                        kind="diff_removed_line",
                        location=Location(file=current_file, line=old_line_no, commit_sha=commit_sha),
                        data={"line_content": content},
                        raw_reference=raw_line,
                    )
                )
                old_line_no += 1

            else:
                old_line_no += 1
                new_line_no += 1

        return observations

    @staticmethod
    def _extract_commit_sha(raw_content: str) -> Optional[str]:
        index_match = re.search(r"^index\s+([0-9a-f]{7,40})", raw_content, re.MULTILINE)
        if index_match:
            return index_match.group(1)
        return None
