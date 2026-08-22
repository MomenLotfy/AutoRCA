from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional

from extractors.base import Observation
from config.rules_config import RulesConfig

PRODUCER_ID = "evidence_builder"
PRODUCER_VERSION = "1.0.0"


class EvidenceBuilderError(ValueError):
    pass


class EvidenceIdGenerator:
    def __init__(self, start: int = 1) -> None:
        if start < 1:
            raise ValueError("start يجب أن يكون 1 أو أكبر.")
        self._counter = start

    def next_id(self) -> str:
        current = self._counter
        self._counter += 1
        return f"E{current}"


class EvidenceBuilder:
    def __init__(self, rules_config: RulesConfig, taxonomy_index: Dict[str, dict]) -> None:
        self._rules = rules_config.classification_rules
        self._taxonomy_index = taxonomy_index

    def build_evidence(
        self,
        analysis_id: str,
        observations: List[Observation],
        id_generator: Optional[EvidenceIdGenerator] = None,
    ) -> List[dict]:
        id_generator = id_generator or EvidenceIdGenerator()
        evidence_list: List[dict] = []

        for observation in observations:
            matching_rule = self._find_matching_rule(observation)
            if matching_rule is None:
                continue

            failure_type_id = matching_rule["produces_failure_type_id"]
            taxonomy_entry = self._taxonomy_index.get(failure_type_id)
            if taxonomy_entry is None:
                raise EvidenceBuilderError(
                    f"classification_rule '{matching_rule['id']}' تنتج "
                    f"failure_type_id='{failure_type_id}' غير موجود في taxonomy_index."
                )

            evidence_list.append(
                {
                    "id": id_generator.next_id(),
                    "analysis_id": analysis_id,
                    "schema_version": 1,
                    "observation_id": observation.id,
                    "failure_type_id": failure_type_id,
                    "classification_rule_id": matching_rule["id"],
                    "type": taxonomy_entry["type"],
                    "source": observation.source,
                    "classification_method": matching_rule["classification_method"],
                    "producer_id": PRODUCER_ID,
                    "producer_version": PRODUCER_VERSION,
                    "data": dict(observation.data),
                    "raw_reference": observation.raw_reference,
                    "extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )

        return evidence_list

    def _find_matching_rule(self, observation: Observation) -> Optional[dict]:
        for rule in self._rules:
            if rule["observation_kind"] != observation.kind:
                continue
            if rule["source"] != observation.source:
                continue
            if self._conditions_match(rule.get("conditions", []), observation):
                return rule
        return None

    @staticmethod
    def _conditions_match(conditions: List[dict], observation: Observation) -> bool:
        if not conditions:
            return True
        for condition in conditions:
            value = EvidenceBuilder._resolve_field(condition["field"], observation)
            if value is None:
                return False
            if not re.search(condition["matches"], str(value)):
                return False
        return True

    @staticmethod
    def _resolve_field(field_path: str, observation: Observation):
        section, _, key = field_path.partition(".")
        if section == "location":
            return getattr(observation.location, key, None)
        if section == "data":
            return observation.data.get(key)
        raise EvidenceBuilderError(
            f"مسار حقل غير مدعوم في classification_rules: '{field_path}'."
        )
