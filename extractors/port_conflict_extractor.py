from __future__ import annotations

import re
from typing import List, Optional

from extractors.base import BaseExtractor, ExtractionContext, Location, Observation
from extractors.registry import ExtractorMetadata, registry

_NODE_EADDRINUSE_PATTERN = re.compile(
    r"EADDRINUSE.*?address already in use\s+"
    r"(?:[\d.:]*?:)?(?P<port>\d{2,5})\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_PYTHON_ADDRESS_IN_USE_PATTERN = re.compile(
    r"OSError:\s*\[Errno 98\]\s*Address already in use",
    re.IGNORECASE,
)
_NEARBY_PORT_PATTERN = re.compile(r"(?:port|:)\s*=?\s*(?P<port>\d{2,5})\b")


@registry.register(
    ExtractorMetadata(
        extractor_id="port_conflict_docker_extractor",
        version="1.0.0",
        source="docker_output",
        produces_kinds=("address_in_use_error",),
        description=(
            "يستخلص رسائل تعارض المنفذ (EADDRINUSE / Address already in "
            "use) من مخرجات Docker."
        ),
    )
)
class PortConflictDockerExtractor(BaseExtractor):
    EXTRACTOR_ID = "port_conflict_docker_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw_content = context.raw_content

        if not raw_content or not raw_content.strip():
            return observations

        seen_signatures: set[str] = set()

        for match in _NODE_EADDRINUSE_PATTERN.finditer(raw_content):
            port = match.group("port")
            signature = f"node:{port}"
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)

            observations.append(
                self.build_observation(
                    context=context,
                    kind="address_in_use_error",
                    location=Location(),
                    data={"port": int(port)},
                    raw_reference=match.group(0).strip(),
                )
            )

        for match in _PYTHON_ADDRESS_IN_USE_PATTERN.finditer(raw_content):
            port = self._find_nearby_port(raw_content, match.start())
            signature = f"python:{port if port is not None else match.start()}"
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)

            data: dict[str, object] = {}
            if port is not None:
                data["port"] = port

            observations.append(
                self.build_observation(
                    context=context,
                    kind="address_in_use_error",
                    location=Location(),
                    data=data or {"port": None},
                    raw_reference=match.group(0),
                )
            )

        return observations

    @staticmethod
    def _find_nearby_port(raw_content: str, match_start_index: int) -> Optional[int]:
        window_start = max(0, match_start_index - 200)
        preceding_text = raw_content[window_start:match_start_index]

        matches = list(_NEARBY_PORT_PATTERN.finditer(preceding_text))
        if not matches:
            return None

        return int(matches[-1].group("port"))
