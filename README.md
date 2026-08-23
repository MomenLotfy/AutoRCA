# AutoRCA — Evidence-Based Root Cause Analysis Engine

نظام تحليل جذري لأعطال CI/CD مبني على مبدأ واحد: **الـ LLM لا يختار السبب
الجذري — هو فقط يشرحه.**

```
Raw Inputs → Observations → Evidence → Rule Engine → Hypotheses
   → Decision Rules → Selected Root Cause → LLM → Explanation + Fix + PR
```

## التشغيل

```bash
pip install -r requirements.txt
pytest tests/ -v
```

62 اختبار يمرّون بنجاح، مقسّمين لسبع طبقات:
- `test_rules_config.py` (10): تحقق Fail Fast من كل خلل بنيوي محتمل في
  rules.config.json — مكان واحد فقط يكتشف كل هذه الأخطاء.
- `test_smoke.py` (14): سيناريوهات end-to-end + اختبارات regression،
  بما فيها اختبار order-independence لبَق حقيقي اتصحح (راجع Bug الرابع
  في TECHNICAL_DEBT.md).
- `test_rca_request_builder.py` (3): بناء RCARequest + severity_policy الفعلي.
- `test_schema_validation.py` (13): تحقق آلي عبر `jsonschema` أن كل مخرج
  فعلي يطابق `schemas/*.json` حرفيًا.
- `test_final_rca_validator.py` (9): تحقق أن الـ LLM لم يخالف القرار الحتمي.
- `test_collectors_and_cli.py` (11): تشغيل GitCollector/FileCollector/CLI
  فعليًا على git repository حقيقي (مبني داخل الاختبار نفسه بـ commit
  حقيقي)، وليس على نصوص مصطنعة.
- `test_llm_integration.py` (5): التحقق من عزل RCARequest، وسياسة الـ prompt،
  ومرور FinalRCAValidator، والفشل الصريح عند تعطل مزود الـ LLM أو غياب
  credentials.

راجع `docs/TECHNICAL_DEBT.md` لتوثيق كل خطأ حقيقي تم اكتشافه وتصحيحه
أثناء التطوير الفعلي.

## تشغيل CLI على حادثة حقيقية

```bash
python3 -m cli.main analyze \
  --repo /path/to/real/git/repo \
  --environment production \
  --traceback /path/to/real/traceback.txt \
  [--commit <sha>] [--docker-log ...] [--ci-log ...] [--full-name org/repo]
```

الفرق عن `demo_run.py`: البيانات هنا بتيجي من git diff حقيقي (subprocess
على repo حقيقي) وملفات log حقيقية على القرص — مش نصوص مكتوبة يدويًا في
الكود. النطاق الحالي محدود لـ Git محلي + ملفات محلية فقط؛ الاتصال الحي
بـ GitHub Actions API / Docker daemon / Kubernetes مسجّل في
`docs/TECHNICAL_DEBT.md` كخطوة تالية واضحة.

## طبقة شرح LLM (Phase 1)

القرار لا يزال حتميًا بالكامل. عند استخدام الخيار `--llm` يصبح التدفق:

```
Real Git/files → AnalysisPipeline → RCARequest → LLM
                                            → FinalRCA schema
                                            → FinalRCAValidator → JSON result
```

الـ LLM يستقبل فقط `RCARequest` المقصوص والمبني بواسطة
`RCARequestBuilder`، ولا يستقبل مصادر الحادث الخام كوسائط منفصلة. ولا يوجد
fallback إلى Fake أو نتيجة ثابتة في مسار التشغيل الحقيقي.

شغّل المسار الحقيقي بعد ضبط الإعدادات في البيئة (لا تضع المفتاح في Git):

```bash
export AUTORCA_LLM_PROVIDER=openai-compatible
export AUTORCA_LLM_MODEL=<model-name>
export AUTORCA_LLM_API_KEY=<secret>
python3 -m cli.main analyze \
  --repo /path/to/real/git/repo \
  --environment production \
  --traceback /path/to/real/traceback.txt \
  --llm
```

يدعم `AUTORCA_LLM_BASE_URL` بوابات OpenAI-compatible، وقيمته الافتراضية
`https://api.openai.com/v1`. عند غياب المفتاح أو النموذج يفشل الأمر بوضوح.
الاختبارات تستخدم `FakeLLMClient` داخل `tests/` فقط ولا تحتاج إنترنت أو API.

## السيناريوهات الحقيقية (Phase 3)

ينشئ هذا الأمر ثلاثة incidents مؤقتة من مصادر حقيقية ثم يشغّل عليها
`GitCollector` و`FileCollector` والـ pipeline:

```bash
python3 real_scenarios.py
```

السيناريوهات هي: commit حقيقي يحذف `PORT` مع traceback ناتج من تشغيل Python،
استيراد package غير موجودة مع `ModuleNotFoundError` حقيقي، وعملية server ثانية
تحاول استخدام منفذ محجوز وتحصل على `Address already in use`. يطبع التقرير
commit SHA الحقيقي والملف والـ command المستخدم. بدون `--llm` تكون نتيجة LLM
موسومة حرفيًا `NOT VERIFIED` ولا تُستخدم نتيجة بديلة.

لتشغيل المسار الكامل مع مزود LLM حقيقي:

```bash
python3 real_scenarios.py --llm
```

إذا لم تكن credentials مهيأة، يفشل الأمر بوضوح ولا يدّعي اكتمال التحقق.

## الاستخدام البرمجي

```python
from pipeline import AnalysisPipeline, PipelineInput

pipeline = AnalysisPipeline.from_config_files(
    rules_config_path="rules/rules.config.json",
    taxonomy_path="taxonomy/taxonomy.yaml",
)

result = pipeline.run(
    PipelineInput(
        analysis_id="AR20260716-001",
        sources={
            "traceback": open("traceback.txt").read(),
            "git_diff": open("diff.patch").read(),
        },
    )
)

print(result.selected)          # Hypothesis المختارة أو None
print(result.evidence_list)     # كل الأدلة المستخلصة
print(result.hypotheses)        # كل الفرضيات (selected/rejected/low_confidence)
```

## هيكل المشروع

```
autorca/
├── taxonomy/taxonomy.yaml
├── schemas/                     # عقود JSON Schema (مُتحقَّق منها آليًا)
├── rules/rules.config.json      # كل الأوزان والقواعد والحدود وfix_hints
├── config/rules_config.py       # نقطة تحميل وتحقق موحّدة
├── collectors/                  # مصادر حقيقية
│   ├── git_collector.py         # git diff فعلي عبر subprocess
│   └── file_collector.py        # قراءة ملفات log/traceback حقيقية
├── extractors/                  # Raw Input → Observation
├── evidence/evidence_builder.py # Observation → Evidence
├── engine/
│   ├── rule_engine.py           # Evidence → Hypothesis
│   └── scoring_engine.py        # raw score → confidence
├── rca_request/rca_request_builder.py  # Hypothesis المختارة → حمولة الـ LLM
├── validation/final_rca_validator.py   # يتحقق من التزام مخرج الـ LLM
├── reporting/incident_report_renderer.py  # تقرير نصي مقروء (حتمي بالكامل)
├── cli/main.py                  # نقطة الدخول الحقيقية: autorca analyze
├── pipeline.py                  # نقطة الدخول الموحّدة (Orchestrator)
├── demo_run.py                  # سكريبت تعليمي ببيانات ثابتة (وليس CLI الحقيقي)
├── tests/
│   ├── test_rules_config.py
│   ├── test_smoke.py
│   ├── test_rca_request_builder.py
│   ├── test_schema_validation.py
│   ├── test_final_rca_validator.py
│   └── test_collectors_and_cli.py
└── docs/TECHNICAL_DEBT.md
```

## أنواع الأعطال المدعومة

| الرمز | النوع | الحالة |
|---|---|---|
| FT001 | Missing Environment Variable | ✅ مطبَّق بالكامل |
| FT002 | Missing Dependency | ✅ مطبَّق بالكامل |
| FT003 | Port Conflict | ✅ مطبَّق بالكامل |
| FT004–FT010 | أنواع أخرى | 🔶 معرَّفة في Taxonomy فقط |

## الخطوات التالية (غير مكتملة بعد)
- ربط LLM حقيقي بدل `fix_hints` الثابتة (المكان جاهز: `rca_request_builder.py` → `final_rca_validator.py`)
- GitHub Actions API integration (استدعاء حي بدل ملفات محفوظة يدويًا)
- Docker daemon / Kubernetes Collectors (لجلب logs مباشرة من بيئة التشغيل)
- `models/*.py` — تمثيل Python كامل مطابق لـ schemas/*.json
