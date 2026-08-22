"""
rca_request/rca_request_builder.py
-----------------------------------------------------------------------------
يحوّل PipelineResult (من pipeline.py) إلى RCARequest — الحمولة المختصرة
التي تُرسل للـ LLM. هذا هو الحد الفاصل الرسمي بين الجزء الحتمي والجزء
التوليدي: بعد هذا الملف، لا يوجد أي منطق حساب أو تصنيف إضافي، فقط تنسيق
(formatting) لما قرره الـ RuleEngine بالفعل.

مسؤوليات محصورة عمدًا:
- لا يُعاد حساب score أو confidence هنا — يُقرآن من selected_hypothesis
  ويُمرَّران لـ ScoringEngine.compute_confidence فقط للتطبيع.
- resolved_severity يُحسب عبر RuleEngine.resolve_severity (الذي يقرأ
  severity_policy من rules.config.json)، وليس بمنطق مستقل هنا.
- diff_excerpt وlog_excerpt يُقصَّان بحد أقصى لعدد الأسطر (truncation)
  لضبط حجم الـ prompt، ولا يُرسل أي نص خام كامل بدون قص.
- summary لكل دليل مختصر (EvidenceSummary) يُبنى بدمج taxonomy label مع
  evidence.data — هذا هو المكان الوحيد المسموح فيه بتوليد نص من بيانات،
  وهو حتمي بالكامل (بدون LLM أو heuristic).
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from engine.rule_engine import Hypothesis, RuleEngine
from engine.scoring_engine import ScoringEngine
from config.rules_config import RulesConfig


class RCARequestBuilderError(ValueError):
    pass


@dataclass(frozen=True)
class GenerationOptions:
    include_pr_diff: bool = True
    include_incident_report: bool = True
    include_fix_steps: bool = True

    def to_dict(self) -> Dict[str, bool]:
        return {
            "include_pr_diff": self.include_pr_diff,
            "include_incident_report": self.include_incident_report,
            "include_fix_steps": self.include_fix_steps,
        }


@dataclass(frozen=True)
class RepositoryContext:
    full_name: str
    branch: str
    commit_sha: str
    environment: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "full_name": self.full_name,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "environment": self.environment,
        }


@dataclass(frozen=True)
class RCARequest:
    schema_version: int
    analysis_id: str
    repository_context: Dict[str, str]
    selected_hypothesis: Dict[str, object]
    confidence: float
    supporting_evidence: List[Dict[str, object]]
    contradicting_evidence: List[Dict[str, object]]
    excluded_hypotheses: List[Dict[str, object]]
    diff_excerpt: str
    log_excerpt: str
    generation_options: Dict[str, bool]
    built_at: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "analysis_id": self.analysis_id,
            "repository_context": self.repository_context,
            "selected_hypothesis": self.selected_hypothesis,
            "confidence": self.confidence,
            "supporting_evidence": self.supporting_evidence,
            "contradicting_evidence": self.contradicting_evidence,
            "excluded_hypotheses": self.excluded_hypotheses,
            "diff_excerpt": self.diff_excerpt,
            "log_excerpt": self.log_excerpt,
            "generation_options": self.generation_options,
        }


class RCARequestBuilder:
    """
    يُنشأ بنسخة واحدة من rules_config وtaxonomy_index (نفس الشكل المستخدم
    في AnalysisPipeline)، بالإضافة إلى RuleEngine وScoringEngine (يمكن
    تمريرهما جاهزين من AnalysisPipeline القائم لتفادي إعادة تحميل
    rules_config مرتين).
    """

    def __init__(
        self,
        rule_engine: RuleEngine,
        scoring_engine: ScoringEngine,
        taxonomy_index: Dict[str, dict],
        rules_config: RulesConfig,
    ) -> None:
        self._rule_engine = rule_engine
        self._scoring_engine = scoring_engine
        self._taxonomy_index = taxonomy_index

        # لا يوجد تحقق هنا — RulesConfig.from_dict/from_file يضمن مسبقًا
        # أن rca_request_limits موجود وبه كل المفاتيح المطلوبة.
        limits = rules_config.rca_request_limits
        self._max_diff_lines = int(limits["max_diff_lines"])
        self._max_log_lines = int(limits["max_log_lines"])
        self._log_context_window = int(limits["log_context_window"])

    def build(
        self,
        *,
        analysis_id: str,
        selected: Hypothesis,
        all_hypotheses: List[Hypothesis],
        evidence_list: List[dict],
        repository_context: RepositoryContext,
        diff_source: Optional[str] = None,
        log_source: Optional[str] = None,
        generation_options: Optional[GenerationOptions] = None,
    ) -> RCARequest:
        """
        selected: يجب أن يكون status == "selected". أي حالة أخرى (candidate,
        rejected, low_confidence) لا يجوز أن تصل لهذا الـ builder — القرار
        بعدم إرسال طلب للـ LLM عند low_confidence مسؤولية الطبقة المستدعية
        (مثال: FastAPI endpoint)، وليس هذا الملف.
        """
        if selected.status != "selected":
            raise RCARequestBuilderError(
                f"لا يجوز بناء RCARequest من فرضية status='{selected.status}'. "
                "فقط الفرضيات بحالة 'selected' مسموح تمريرها لهذا الـ builder."
            )

        generation_options = generation_options or GenerationOptions()
        evidence_by_id = {e["id"]: e for e in evidence_list}

        supporting_evidence = [
            self._build_evidence_summary(evidence_by_id[link.evidence_id])
            for link in selected.links
            if link.relation == "supports"
        ]
        contradicting_evidence = [
            self._build_evidence_summary(evidence_by_id[link.evidence_id])
            for link in selected.links
            if link.relation == "contradicts"
        ]

        confidence = self._scoring_engine.compute_confidence(selected.score)
        resolved_severity = self._rule_engine.resolve_severity(
            failure_type_id=selected.failure_type_id,
            context={"environment": repository_context.environment},
            default_severity=self._taxonomy_index[selected.failure_type_id]["default_severity"],
        )

        selected_hypothesis_payload = {
            "id": selected.id,
            "failure_type_id": selected.failure_type_id,
            "label": selected.label,
            "description": selected.description,
            "score": selected.score,
            "resolved_severity": resolved_severity,
        }

        excluded_hypotheses = [
            {
                "id": h.id,
                "label": h.label,
                "score": h.score,
                "status": h.status,
            }
            for h in all_hypotheses
            if h.id != selected.id and h.status in ("rejected", "low_confidence")
        ]

        diff_excerpt = self._truncate(diff_source or "", self._max_diff_lines)
        log_excerpt = self._build_log_excerpt(
            log_source or "", supporting_evidence + contradicting_evidence
        )

        return RCARequest(
            schema_version=1,
            analysis_id=analysis_id,
            repository_context=repository_context.to_dict(),
            selected_hypothesis=selected_hypothesis_payload,
            confidence=confidence,
            supporting_evidence=supporting_evidence,
            contradicting_evidence=contradicting_evidence,
            excluded_hypotheses=excluded_hypotheses,
            diff_excerpt=diff_excerpt,
            log_excerpt=log_excerpt,
            generation_options=generation_options.to_dict(),
            built_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )

    def _build_evidence_summary(self, evidence: dict) -> Dict[str, object]:
        failure_type_id = evidence["failure_type_id"]
        taxonomy_entry = self._taxonomy_index.get(failure_type_id)
        if taxonomy_entry is None:
            raise RCARequestBuilderError(
                f"failure_type_id '{failure_type_id}' غير موجود في taxonomy_index."
            )

        summary = self._render_summary_text(taxonomy_entry, evidence["data"])

        return {
            "evidence_id": evidence["id"],
            "failure_type_id": failure_type_id,
            "summary": summary,
            "raw_reference": evidence.get("raw_reference"),
        }

    @staticmethod
    def _render_summary_text(taxonomy_entry: dict, data: Dict[str, object]) -> str:
        """
        يبني جملة قصيرة قابلة للقراءة من label الـ taxonomy + الحقول
        البنيوية في evidence.data. حتمي بالكامل — مفيش أي استدعاء LLM أو
        heuristic هنا، فقط تنسيق نصي مباشر.
        """
        label = taxonomy_entry["type"]

        if "key" in data:
            return f"Missing environment variable: {data['key']}"
        if "module" in data:
            return f"Missing module/package: {data['module']}"
        if "port" in data and data["port"] is not None:
            return f"Port already in use: {data['port']}"
        if "line_content" in data:
            return f"Removed line related to {label}: {data['line_content'].strip()}"

        # fallback عام: لا يخترع تفاصيل غير موجودة في data.
        return f"Evidence of type {label}"

    @staticmethod
    def _truncate(text: str, max_lines: int) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        truncated = lines[:max_lines]
        remaining = len(lines) - max_lines
        truncated.append(f"... ({remaining} more lines truncated)")
        return "\n".join(truncated)

    def _build_log_excerpt(
        self, log_source: str, all_evidence_summaries: List[Dict[str, object]]
    ) -> str:
        """
        يبني مقطع لوج مركّز حول raw_reference الخاصة بالأدلة المرسلة، بدل
        إرسال اللوج الكامل. لو raw_reference لم يُعثر عليه في log_source
        (لأنه جاء من traceback/git_diff وليس من اللوج نفسه)، يُتجاهل بصمت.
        نافذة السياق (self._log_context_window) وحد القص
        (self._max_log_lines) مصدرهما rca_request_limits في
        rules.config.json حصريًا — لا يوجد أي رقم مكتوب هنا.
        """
        if not log_source:
            return ""

        lines = log_source.splitlines()
        selected_line_indexes: set[int] = set()

        for summary in all_evidence_summaries:
            raw_ref = summary.get("raw_reference")
            if not raw_ref:
                continue
            for i, line in enumerate(lines):
                if raw_ref.strip() and raw_ref.strip() in line:
                    window_start = max(0, i - self._log_context_window)
                    window_end = min(len(lines), i + self._log_context_window + 1)
                    selected_line_indexes.update(range(window_start, window_end))

        if not selected_line_indexes:
            # لا يوجد أي دليل مصدره اللوج نفسه — يُرجع مقطع مقصوص من أول
            # اللوج كحد أدنى، بدل نص فارغ تمامًا.
            return self._truncate(log_source, self._max_log_lines)

        ordered_indexes = sorted(selected_line_indexes)
        excerpt_lines = [lines[i] for i in ordered_indexes]
        return self._truncate("\n".join(excerpt_lines), self._max_log_lines)
