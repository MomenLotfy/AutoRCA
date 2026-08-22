from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from config.rules_config import RulesConfig
from engine.rule_engine import Hypothesis, RuleEngine
from engine.scoring_engine import ScoringEngine
from evidence.evidence_builder import EvidenceBuilder, EvidenceIdGenerator
from extractors.base import ExtractionContext, ObservationIdGenerator, Observation
from extractors.registry import registry

import extractors.missing_env_extractor  # noqa: F401
import extractors.missing_dependency_extractor  # noqa: F401
import extractors.port_conflict_extractor  # noqa: F401
import extractors.diff_extractor  # noqa: F401


class PipelineConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PipelineInput:
    analysis_id: str
    sources: Dict[str, str]


@dataclass(frozen=True)
class PipelineResult:
    analysis_id: str
    observations: List[Observation]
    evidence_list: List[dict]
    hypotheses: List[Hypothesis]

    @property
    def selected(self) -> Optional[Hypothesis]:
        for hypothesis in self.hypotheses:
            if hypothesis.status == "selected":
                return hypothesis
        return None


class AnalysisPipeline:
    def __init__(self, rules_config: RulesConfig, taxonomy_index: Dict[str, dict]) -> None:
        self._rules_config = rules_config
        self._taxonomy_index = taxonomy_index
        self._rule_engine = RuleEngine(rules_config)
        self._scoring_engine = ScoringEngine(rules_config)
        self._evidence_builder = EvidenceBuilder(rules_config, taxonomy_index)

    @classmethod
    def from_config_files(cls, rules_config_path, taxonomy_path) -> "AnalysisPipeline":
        rules_config = RulesConfig.from_file(rules_config_path)
        taxonomy_index = cls._load_taxonomy_index(Path(taxonomy_path))
        return cls(rules_config, taxonomy_index)

    @staticmethod
    def _load_taxonomy_index(path: Path) -> Dict[str, dict]:
        if not path.exists():
            raise PipelineConfigError(f"taxonomy.yaml غير موجود في المسار: {path}")
        with path.open(encoding="utf-8") as f:
            taxonomy = yaml.safe_load(f)

        return {
            entry["id"]: {
                "type": entry["type"],
                "category": entry["category"],
                "default_severity": entry["default_severity"],
            }
            for entry in taxonomy["failure_types"]
        }

    def run(self, pipeline_input: PipelineInput) -> PipelineResult:
        observations = self._run_extraction(pipeline_input)
        evidence_list = self._evidence_builder.build_evidence(
            analysis_id=pipeline_input.analysis_id,
            observations=observations,
            id_generator=EvidenceIdGenerator(),
        )
        hypotheses = self._rule_engine.build_hypotheses(
            analysis_id=pipeline_input.analysis_id,
            evidence_list=evidence_list,
        )

        return PipelineResult(
            analysis_id=pipeline_input.analysis_id,
            observations=observations,
            evidence_list=evidence_list,
            hypotheses=hypotheses,
        )

    def _run_extraction(self, pipeline_input: PipelineInput) -> List[Observation]:
        id_generator = ObservationIdGenerator()
        all_observations: List[Observation] = []

        for source_name, raw_content in pipeline_input.sources.items():
            extractor_classes = registry.get_extractor_classes_for_source(source_name)
            context = ExtractionContext(
                analysis_id=pipeline_input.analysis_id,
                raw_content=raw_content,
                id_generator=id_generator,
            )
            for extractor_cls in extractor_classes:
                extractor = extractor_cls()
                all_observations.extend(extractor.extract(context))

        return all_observations

    def resolve_severity(self, failure_type_id: str, context: Dict[str, object]) -> str:
        default_severity = self._taxonomy_index[failure_type_id]["default_severity"]
        return self._rule_engine.resolve_severity(failure_type_id, context, default_severity)

    def compute_confidence(self, raw_score: float) -> float:
        return self._scoring_engine.compute_confidence(raw_score)
