"""
validation/final_rca_validator.py
-----------------------------------------------------------------------------
Validator محض — لا يتخذ أي قرار، فقط يتحقق أن مخرج الـ LLM (FinalRCA)
لم يخالف القرار الحتمي الذي وصل إليه RuleEngine بالفعل عبر RCARequest.

هذا الملف لا يعيد تشغيل RuleEngine ولا يحسب score من جديد — لو احتاج
لذلك، فهذا مؤشر على اختلاط المسؤوليات بين طبقتين. كل تحقق هنا هو مقارنة
مباشرة بين قيمتين موجودتين بالفعل (واحدة في RCARequest، وأخرى في
FinalRCA المُدَّعى)، أو تحقق مرجعي بسيط (هل evidence_id موجود؟)، أو
تفويض لأداة تحقق عامة جاهزة (jsonschema).

التحققات الستة المطلوبة:
1. selected_hypothesis_id في FinalRCA == RCARequest.selected_hypothesis.id
2. كل evidence_id مُستشهَد به في FinalRCA موجود فعليًا في RCARequest
3. confidence في FinalRCA == RCARequest.confidence حرفيًا
4. resolved_severity في incident_report == RCARequest.selected_hypothesis.resolved_severity
5. FinalRCA يطابق final_rca.schema.json بالكامل (jsonschema)
6. توافق pull_request/incident_report مع generation_options (nullability)

لا يوجد تحقق سابع بخصوص "هل failure_type_id جديد؟" لأن FinalRCA لا يحمل
failure_type_id مستقلًا أصلًا (schema الحالي لا يطلبه) — فقط
selected_hypothesis_id، والتحقق رقم 1 كافٍ لضمان عدم تغييره.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import jsonschema

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"


class FinalRCAValidationError(ValueError):
    """
    تُرفع عند فشل أي تحقق. تحمل قائمة violations كاملة (وليس أول خطأ فقط)
    حتى يقدر المستدعي (مثال: FastAPI endpoint يقرر إعادة الطلب للـ LLM)
    يشوف كل المخالفات دفعة واحدة بدل تكرار المحاولة خطأ بخطأ.
    """

    def __init__(self, violations: List[str]) -> None:
        self.violations = violations
        super().__init__("؛ ".join(violations))


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    violations: List[str]


class FinalRCAValidator:
    """
    يُنشأ مرة واحدة (يحمّل final_rca.schema.json وقت الإنشاء)، ويُستخدم
    لكل عملية تحقق لاحقة — بدون حالة خاصة بتحليل معين.
    """

    def __init__(self) -> None:
        with (SCHEMAS_DIR / "final_rca.schema.json").open(encoding="utf-8") as f:
            self._final_rca_schema = json.load(f)

    def validate(self, rca_request: Dict[str, object], final_rca: Dict[str, object]) -> ValidationResult:
        """
        rca_request: نفس الحمولة التي أُرسلت للـ LLM (RCARequest.to_dict()).
        final_rca: الاستجابة المُدَّعاة من الـ LLM، كـ dict خام (قبل أي ثقة بها).

        لا يرفع استثناء — يرجع ValidationResult دائمًا؛ القرار بشأن ماذا
        يحدث بعد الفشل (رفض، إعادة طلب، تسجيل) مسؤولية المستدعي، وليست
        مسؤولية الـ Validator.
        """
        violations: List[str] = []

        violations.extend(self._validate_schema_conformance(final_rca))

        # التحققات التالية تفترض توفر البنية الأساسية على الأقل. لو فشل
        # التحقق البنيوي (schema) بشكل جوهري (حقول أساسية غائبة)، نوقف هنا
        # لتفادي KeyError أثناء التحققات الدلالية التالية.
        if not violations:
            violations.extend(self._validate_hypothesis_id_unchanged(rca_request, final_rca))
            violations.extend(self._validate_confidence_unchanged(rca_request, final_rca))
            violations.extend(self._validate_cited_evidence_exists(rca_request, final_rca))
            violations.extend(self._validate_severity_unchanged(rca_request, final_rca))
            violations.extend(self._validate_generation_options_nullability(rca_request, final_rca))

        return ValidationResult(is_valid=(len(violations) == 0), violations=violations)

    def validate_or_raise(self, rca_request: Dict[str, object], final_rca: Dict[str, object]) -> None:
        result = self.validate(rca_request, final_rca)
        if not result.is_valid:
            raise FinalRCAValidationError(result.violations)

    # ------------------------------------------------------------------
    # التحققات الستة — كل واحد مسؤول عن مقارنة واحدة فقط
    # ------------------------------------------------------------------

    def _validate_schema_conformance(self, final_rca: Dict[str, object]) -> List[str]:
        validator = jsonschema.Draft7Validator(self._final_rca_schema)
        errors = sorted(validator.iter_errors(final_rca), key=lambda e: e.path)
        return [f"مخالفة schema: {e.message} (المسار: {list(e.path)})" for e in errors]

    @staticmethod
    def _validate_hypothesis_id_unchanged(rca_request: dict, final_rca: dict) -> List[str]:
        expected_id = rca_request["selected_hypothesis"]["id"]
        actual_id = final_rca.get("selected_hypothesis_id")
        if actual_id != expected_id:
            return [
                f"الـ LLM غيّر الفرضية المختارة: المتوقع '{expected_id}', "
                f"الفعلي '{actual_id}'. الـ LLM لا يملك صلاحية تغيير القرار."
            ]
        return []

    @staticmethod
    def _validate_confidence_unchanged(rca_request: dict, final_rca: dict) -> List[str]:
        expected_confidence = rca_request["confidence"]
        actual_confidence = final_rca.get("confidence")
        if actual_confidence != expected_confidence:
            return [
                f"الـ LLM غيّر confidence: المتوقع {expected_confidence}, "
                f"الفعلي {actual_confidence}. confidence يُنسَخ حرفيًا ولا يُعاد حسابه."
            ]
        return []

    @staticmethod
    def _validate_cited_evidence_exists(rca_request: dict, final_rca: dict) -> List[str]:
        known_evidence_ids = {e["evidence_id"] for e in rca_request["supporting_evidence"]}
        known_evidence_ids |= {e["evidence_id"] for e in rca_request["contradicting_evidence"]}

        violations: List[str] = []

        cited_in_explanation = set(final_rca.get("explanation", {}).get("cited_evidence_ids", []))
        unknown_in_explanation = cited_in_explanation - known_evidence_ids
        if unknown_in_explanation:
            violations.append(
                f"explanation.cited_evidence_ids يحتوي معرفات غير موجودة في "
                f"RCARequest: {sorted(unknown_in_explanation)}"
            )

        cited_in_fix = set(final_rca.get("fix", {}).get("cited_evidence_ids", []))
        unknown_in_fix = cited_in_fix - known_evidence_ids
        if unknown_in_fix:
            violations.append(
                f"fix.cited_evidence_ids يحتوي معرفات غير موجودة في "
                f"RCARequest: {sorted(unknown_in_fix)}"
            )

        return violations

    @staticmethod
    def _validate_severity_unchanged(rca_request: dict, final_rca: dict) -> List[str]:
        incident_report = final_rca.get("incident_report")
        if incident_report is None:
            return []

        expected_severity = rca_request["selected_hypothesis"]["resolved_severity"]
        actual_severity = incident_report.get("severity")
        if actual_severity != expected_severity:
            return [
                f"الـ LLM غيّر severity في incident_report: المتوقع "
                f"'{expected_severity}', الفعلي '{actual_severity}'. severity "
                "يُنسخ من resolved_severity ولا يُعاد تقييمه."
            ]
        return []

    @staticmethod
    def _validate_generation_options_nullability(rca_request: dict, final_rca: dict) -> List[str]:
        violations: List[str] = []
        generation_options = rca_request["generation_options"]

        pr_expected_null = not generation_options["include_pr_diff"]
        pr_is_null = final_rca.get("pull_request") is None
        if pr_expected_null and not pr_is_null:
            violations.append(
                "generation_options.include_pr_diff=false لكن pull_request غير null."
            )
        if not pr_expected_null and pr_is_null:
            violations.append(
                "generation_options.include_pr_diff=true لكن pull_request=null."
            )

        report_expected_null = not generation_options["include_incident_report"]
        report_is_null = final_rca.get("incident_report") is None
        if report_expected_null and not report_is_null:
            violations.append(
                "generation_options.include_incident_report=false لكن incident_report غير null."
            )
        if not report_expected_null and report_is_null:
            violations.append(
                "generation_options.include_incident_report=true لكن incident_report=null."
            )

        return violations
