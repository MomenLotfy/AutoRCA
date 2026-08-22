"""
collectors/git_collector.py
-----------------------------------------------------------------------------
Collector حقيقي — بيشغّل git فعليًا على repository حقيقي على القرص، وبيرجّع
نص diff حقيقي (مش نص مكتوب يدويًا). ده الفرق الجوهري بين demo_run.py
(نصوص ثابتة) وبين AutoRCA الحقيقي (بيانات حية من مصدرها).

نطاق هذا الـ Collector محدود عمدًا لـ Git المحلي (subprocess) — وليس GitHub
API عن بعد. الوصول لـ commits/diffs عبر GitHub API (لمستودعات بعيدة بدون
نسخة محلية) مسجّل في TECHNICAL_DEBT.md كخطوة تالية، وليس متاحًا هنا لأنه
يحتاج مصادقة (auth) وشبكة غير متوفرة في بيئة التنفيذ الحالية.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitCollectorError(RuntimeError):
    """تُرفع عند أي فشل في تنفيذ git نفسه (مسار غير صالح، commit غير موجود...)."""


class GitCollector:
    """
    يُنشأ بمسار repository محلي واحد. لا يحتفظ بأي حالة بين استدعاءات
    collect_diff المتعددة — كل استدعاء عملية git مستقلة.
    """

    def __init__(self, repo_path: str) -> None:
        resolved = Path(repo_path).resolve()
        if not resolved.exists():
            raise GitCollectorError(f"المسار غير موجود: {resolved}")
        if not (resolved / ".git").exists():
            raise GitCollectorError(f"المسار ليس git repository: {resolved}")
        self.repo_path = str(resolved)

    def collect_diff(self, commit: str | None = None) -> str:
        """
        لو commit اتحدد: بيرجّع الـ diff الخاص بيه فقط (git show <commit>).
        لو مفيش commit: بيرجّع diff آخر commit في الـ history (HEAD~1..HEAD)
        — أقرب افتراض معقول لـ "آخر تغيير أدى للفشل" لما مفيش commit محدد صراحة.
        """
        if commit:
            args = ["git", "-C", self.repo_path, "show", "--format=", commit]
        else:
            args = ["git", "-C", self.repo_path, "diff", "HEAD~1", "HEAD"]

        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise GitCollectorError(
                f"فشل تنفيذ git diff (commit={commit!r}): {result.stderr.strip()}"
            )
        if not result.stdout.strip():
            raise GitCollectorError(
                f"git رجّع diff فاضي (commit={commit!r}) — تأكد إن الـ commit موجود "
                "وفيه تغييرات فعلية."
            )
        return result.stdout

    def resolve_commit_sha(self, ref: str = "HEAD") -> str:
        result = subprocess.run(
            ["git", "-C", self.repo_path, "rev-parse", ref],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise GitCollectorError(f"فشل تحديد commit sha لـ '{ref}': {result.stderr.strip()}")
        return result.stdout.strip()

    def resolve_branch(self) -> str:
        result = subprocess.run(
            ["git", "-C", self.repo_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise GitCollectorError(f"فشل تحديد اسم الـ branch: {result.stderr.strip()}")
        return result.stdout.strip()
