"""
collectors/file_collector.py
-----------------------------------------------------------------------------
Collector بسيط — بيقرأ نص خام (traceback, docker output, ci log) من ملف
حقيقي على القرص. الغرض منه: في غياب اتصال حي بـ GitHub Actions API (غير
متاح في بيئة التنفيذ الحالية)، المستخدم يقدر يحفظ الـ log كملف نصي حقيقي
(انسخه من الـ CI الفعلي بتاعه) ويشغّل AutoRCA عليه.

هذا Collector لا يخترع أو يعدّل أي محتوى — بيرجّع النص كما هو حرفيًا.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

from pathlib import Path


class FileCollectorError(RuntimeError):
    pass


class FileCollector:
    @staticmethod
    def collect(file_path: str) -> str:
        path = Path(file_path)
        if not path.exists():
            raise FileCollectorError(f"الملف غير موجود: {path}")
        if not path.is_file():
            raise FileCollectorError(f"المسار ليس ملفًا: {path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            raise FileCollectorError(f"الملف فاضي: {path}")
        return content
