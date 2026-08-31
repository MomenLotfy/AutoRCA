from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from config.rules_config import RulesConfig
from engine.correlation_engine import CorrelationEngine, CorrelationGraph
from engine.hypothesis_engine import HypothesisAssessmentResult, HypothesisEngine
from engine.incident_fingerprint import IncidentFingerprint, IncidentFingerprintBuilder
from engine.incident_graph import IncidentGraph, IncidentGraphBuilder
from engine.remediation_engine import RemediationContext, RemediationEngine
from engine.rule_engine import Hypothesis, RuleEngine
from engine.scoring_engine import ScoringEngine
from engine.timeline_engine import IncidentTimeline, TimelineEngine
from evidence.evidence_builder import EvidenceBuilder, EvidenceIdGenerator
from extractors.base import ExtractionContext, ObservationIdGenerator, Observation
from extractors.registry import registry

import extractors.missing_env_extractor  # noqa: F401
import extractors.missing_dependency_extractor  # noqa: F401
import extractors.port_conflict_extractor  # noqa: F401
import extractors.diff_extractor  # noqa: F401
import extractors.docker_event_extractor  # noqa: F401
import extractors.docker_metrics_extractor  # noqa: F401
import extractors.host_metrics_extractor  # noqa: F401
import extractors.elasticsearch_extractor  # noqa: F401
import extractors.prometheus_extractor  # noqa: F401
import extractors.github_change_extractor  # noqa: F401
import extractors.gitlab_change_extractor  # noqa: F401
import extractors.kubernetes_extractor  # noqa: F401
import extractors.cicd_extractor  # noqa: F401


def _filter_observations_by_window(
    observations: List[Observation],
    *,
    start: Optional[dt.datetime],
    end: Optional[dt.datetime],
) -> List[Observation]:
    """Filter Observations whose `extracted_at` lies outside [start, end].

    If `start` or `end` is None, that side is unbounded.

    Observations without a parseable `extracted_at` are kept ONLY when the
    window is fully unbounded. As soon as at least one bound is set, we
    cannot prove an unparseable observation belongs in the window, so it
    is dropped (this matches the Architecture Freeze rule: never
    fabricate timestamps).
    """
    fully_bounded = start is not None and end is not None
    keep_unknown = not fully_bounded

    if start is None and end is None:
        return list(observations)

    out: List[Observation] = []
    for obs in observations:
        ts = _parse_extracted_at(obs.extracted_at)
        if ts is None:
            if keep_unknown:
                out.append(obs)
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        out.append(obs)
    return out


def _parse_extracted_at(value: str) -> Optional[dt.datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


class PipelineConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PipelineInput:
    analysis_id: str
    sources: Dict[str, str]
    environment: str = "unknown"
    commit_sha: Optional[str] = None
    # Phase 1.5 — optional IncidentContext fields. All default to None so
    # existing callers that construct PipelineInput(positionally or with
    # keywords) continue to work. When set, observations whose
    # extracted_at falls outside the window are filtered out at the
    # pipeline boundary (the protected TimelineEngine is untouched).
    incident_start: Optional[dt.datetime] = None
    incident_end: Optional[dt.datetime] = None
    service: Optional[str] = None
    deployment: Optional[str] = None


@dataclass(frozen=True)
class PipelineResult:
    """النتيجة الكاملة للـ pipeline بعد كل المراحل.

    تحتوي على البنية القديمة (observations / evidence / hypotheses) للحفاظ
    على التوافق مع الكود القائم، بالإضافة إلى البنية الجديدة
    (timeline / correlation / graph / fingerprint / remediation /
    hypothesis_assessment).
    """

    analysis_id: str
    observations: List[Observation]
    evidence_list: List[dict]
    hypotheses: List[Hypothesis]
    timeline: IncidentTimeline = None  # type: ignore[assignment]
    correlation: CorrelationGraph = None  # type: ignore[assignment]
    graph: IncidentGraph = None  # type: ignore[assignment]
    fingerprint: Optional[IncidentFingerprint] = None
    remediation: Optional[RemediationContext] = None
    hypothesis_assessment: Optional[HypothesisAssessmentResult] = None

    @property
    def selected(self) -> Optional[Hypothesis]:
        for hypothesis in self.hypotheses:
            if hypothesis.status == "selected":
                return hypothesis
        return None

    def selected_failure_type_id(self) -> Optional[str]:
        if self.selected is not None:
            return self.selected.failure_type_id
        if self.hypothesis_assessment and self.hypothesis_assessment.selected is not None:
            return self.hypothesis_assessment.selected.failure_type_id
        return None


class AnalysisPipeline:
    def __init__(
        self,
        rules_config: RulesConfig,
        taxonomy_index: Dict[str, dict],
    ) -> None:
        self._rules_config = rules_config
        self._taxonomy_index = taxonomy_index
        self._rule_engine = RuleEngine(rules_config)
        self._scoring_engine = ScoringEngine(rules_config)
        self._evidence_builder = EvidenceBuilder(rules_config, taxonomy_index)
        # Modules الجديدة:
        self._timeline_engine = TimelineEngine()
        self._correlation_engine = CorrelationEngine()
        self._graph_builder = IncidentGraphBuilder()
        self._fingerprint_builder = IncidentFingerprintBuilder()
        self._remediation_engine = RemediationEngine(rules_config.fix_hints)
        self._hypothesis_engine = HypothesisEngine(
            rules_config=rules_config,
            rule_engine=self._rule_engine,
            scoring_engine=self._scoring_engine,
        )

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
        started_at = dt.datetime.now(dt.timezone.utc).isoformat()
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

        # Modules الجديدة (لا تكسر البيانات القديمة)
        timeline = self._timeline_engine.build(
            analysis_id=pipeline_input.analysis_id,
            observations=observations,
            analysis_started_at=started_at,
        )
        correlation = self._correlation_engine.correlate(
            analysis_id=pipeline_input.analysis_id,
            observations=observations,
        )

        # تحديد الفرضية المختارة لإنشاء graph/fingerprint/remediation
        selected_hypothesis: Optional[Hypothesis] = None
        for hyp in hypotheses:
            if hyp.status == "selected":
                selected_hypothesis = hyp
                break

        graph = self._graph_builder.build(
            analysis_id=pipeline_input.analysis_id,
            correlation=correlation,
            observations=observations,
            selected_failure_type_id=selected_hypothesis.failure_type_id if selected_hypothesis else None,
            selected_failure_label=selected_hypothesis.label if selected_hypothesis else None,
            commit_sha=pipeline_input.commit_sha,
        )
        fingerprint = self._fingerprint_builder.build(
            analysis_id=pipeline_input.analysis_id,
            observations=observations,
            failure_type_id=selected_hypothesis.failure_type_id if selected_hypothesis else None,
            environment=pipeline_input.environment,
        )
        remediation: Optional[RemediationContext] = None
        if selected_hypothesis is not None:
            remediation = self._remediation_engine.build(
                analysis_id=pipeline_input.analysis_id,
                failure_type_id=selected_hypothesis.failure_type_id,
                evidence_list=evidence_list,
                environment=pipeline_input.environment,
            )
        hypothesis_assessment = self._hypothesis_engine.assess(
            analysis_id=pipeline_input.analysis_id,
            evidence_list=evidence_list,
            observations=observations,
            environment=pipeline_input.environment,
        )

        return PipelineResult(
            analysis_id=pipeline_input.analysis_id,
            observations=observations,
            evidence_list=evidence_list,
            hypotheses=hypotheses,
            timeline=timeline,
            correlation=correlation,
            graph=graph,
            fingerprint=fingerprint,
            remediation=remediation,
            hypothesis_assessment=hypothesis_assessment,
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

        # Phase 1.5 — IncidentContext time-window filter applied at the
        # pipeline boundary so the protected TimelineEngine stays untouched.
        # Observations whose extracted_at falls outside the window are
        # dropped (we never fabricate timestamps). When the window is
        # unbounded on either side, all observations pass.
        if (
            pipeline_input.incident_start is not None
            or pipeline_input.incident_end is not None
        ):
            all_observations = _filter_observations_by_window(
                all_observations,
                start=pipeline_input.incident_start,
                end=pipeline_input.incident_end,
            )

        return all_observations

    def resolve_severity(self, failure_type_id: str, context: Dict[str, object]) -> str:
        default_severity = self._taxonomy_index[failure_type_id]["default_severity"]
        return self._rule_engine.resolve_severity(failure_type_id, context, default_severity)

    def compute_confidence(self, raw_score: float) -> float:
        return self._scoring_engine.compute_confidence(raw_score)
