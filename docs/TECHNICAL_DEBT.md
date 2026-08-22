# Technical Debt — AutoRCA

كل نقطة هنا اتسجلت وقت اتخاذ القرار في التصميم، مش بأثر رجعي.

## TD-001 — Corroboration بدون Deduplication
`RuleEngine._accumulate_scores` بيدي وزن تعزيز لكل Evidence إضافية بعد
أول دليل للفرضية، من غير تحقق إن الأدلة دي مستقلة فعليًا أو مجرد تكرار
لنفس الحدث (مثال: نفس رسالة الخطأ ظاهرة 10 مرات في نفس اللوج).
الحل المقترح: إضافة `deduplication_key` (fingerprint من data + location)
في Evidence، وتجميع الأدلة المتطابقة كدليل واحد قبل حساب corroboration.
غير حرج للـ MVP الحالي لأن الـ Extractors الثلاثة الفعلية بتعمل dedup
داخلي بالفعل (seen_keys/seen_modules/seen_signatures).

## TD-002 — Observation.kind مسطّح (Enum) بدل بنية هرمية
`kind` حاليًا enum مسطّح (key_error, module_not_found_error...). v2
المقترح: بنية هرمية (family + kind) مثل family="python", kind="key_error"
لتفادي انفجار عدد القيم مع نمو النظام.

## TD-003 — ~~عدم وجود اختبار اتساق آلي بين models/ و schemas/~~ [تم الحل جزئيًا]
تم إنشاء `schemas/*.json` فعليًا وإضافة `tests/test_schema_validation.py`
الذي يتحقق آليًا (عبر مكتبة jsonschema) أن كل مخرج فعلي من الـ pipeline
(Observation, Evidence, Hypothesis, RCARequest) يطابق الـ Contract حرفيًا.
هذا الاختبار هو الذي كشف مباشرة نوعية الأخطاء التي فاتت الاختبارات
الوظيفية (راجع Bug الثالث أدناه). ما زال الجزء غير المكتمل: `models/*.py`
(تمثيل Python كامل مطابق للـ schemas) لسه مش مكتوب — حاليًا الكود بيستخدم
dicts وdataclasses يدوية بدل توليد تلقائي من الـ schema.

## TD-004 — Fixtures مضمّنة داخل test_smoke.py
سيناريوهات الاختبار (traceback/git_diff/docker_output) نصوص ثابتة داخل
tests/test_smoke.py، وليست ملفات منفصلة في fixtures/ كما هو مخطط في
بنية المشروع الأصلية. تبسيط متعمد لضمان تشغيل فوري، محتاج إعادة هيكلة
عند الانتقال من الهاكاثون للإنتاج.

## TD-005 — dataclasses بدل pydantic في الـ Extractors
extractors/base.py و engine/rule_engine.py بيستخدموا dataclasses قياسية
بدل pydantic، لتقليل الاعتماديات الخارجية في هذه الطبقة. لو الـ FastAPI
backend هيستخدم pydantic بشكل شامل، قرار التوحيد بين الاتنين لسه مفتوح.

## TD-006 — ~~validation/final_rca_validator.py غير مكتوب~~ [تم الحل]
final_rca_validator.py مكتوب ومختبر بالكامل (9 اختبارات). Validator محض:
لا يتخذ قرارات، لا يعيد تشغيل RuleEngine، فقط يقارن قيم RCARequest مقابل
FinalRCA المُدَّعى + تحقق jsonschema. التحققات الستة: hypothesis_id،
confidence، cited_evidence_ids، resolved_severity،
generation_options↔nullability، وmطابقة schema الكاملة.

## قرار: RulesConfig كطبقة تحميل وتحقق موحّدة
قبل هذا القرار، كانت RuleEngine وScoringEngine وEvidenceBuilder و
RCARequestBuilder كل واحدة تصل لـ rules_config["..."] مباشرة كـ dict خام،
وكل واحدة فيها تحقق بنيوي منفصل ومكرر (مثال: تحقق public_id كان داخل
RuleEngine.__init__، تحقق confidence_normalization.method كان داخل
ScoringEngine.__init__). تم توحيدهم في config/rules_config.py:
RulesConfig.from_dict()/from_file() يحمّل ويتحقق من كل القسم البنيوي
مرة واحدة فقط (top-level keys، public_id صحة وفرادة، مرجعية
hypothesis_rules→hypotheses_catalog، confidence method مدعوم،
rca_request_limits مكتمل)، وكل الطبقات الأخرى تستقبل RulesConfig جاهز
ومُحقَّق عبر attributes مكتوبة (rules_config.hypothesis_rules) بدل
dict indexing. أضيف tests/test_rules_config.py (10 اختبارات) يتحقق أن كل
نوع خلل بنيوي يُرفض في هذا المكان الوحيد.

## Bug مُصحَّح فعليًا أثناء أول تشغيل حقيقي (موثّق هنا للسياق فقط)
عند تشغيل pytest لأول مرة على الكود الفعلي، ظهر أن `_accumulate_scores`
في RuleEngine كان يحسب "أول دليل" بالنسبة لكل classification_rule_id
على حدة، بدل "أول دليل للفرضية ككل" — ما أدى لإعطاء وزن أساسي كامل
لكل دليل مختلف المصدر بدل وزن أساسي واحد + تعزيز، فأنتج score=1.0
(بعد clamp) بدل 0.90 المتوقع. تم التصحيح، وأُضيف اختبار regression
صريح (`test_corroboration_uses_first_evidence_per_hypothesis_not_per_rule`)
لضمان عدم تكرار هذا الخطأ.

## Bug ثالث: rca_request_limits كانت hardcoded في الكود (مُصحَّح)
عند مراجعة rca_request_builder.py يدويًا مقابل قائمة تحقق (checklist)،
ظهر أن DEFAULT_MAX_DIFF_LINES/DEFAULT_MAX_LOG_LINES/
DEFAULT_LOG_CONTEXT_WINDOW كانت ثوابت مكتوبة مباشرة في الكود، بعكس مبدأ
"كل رقم مصدره rules.config.json" المتبع في باقي المشروع. تم التصحيح
بإضافة قسم rca_request_limits في rules.config.json، وRCARequestBuilder
يفشل الآن وقت الإنشاء (Fail Fast) لو القسم مفقود، بدل الاعتماد على قيم
افتراضية صامتة.

## قرار: public_id صريح بدل توليد ترقيم من ترتيب القاموس
بعد تصحيح bug hypothesis.id ("RCmissing_env")، كان الحل الأول المؤقت
يعتمد على ترتيب مفاتيح hypotheses_catalog لتوليد RC1/RC2/RC3 — لكن هذا
غير آمن لأن أي إعادة ترتيب في rules.config.json كانت ستغيّر المعرفات
بصمت. تم الحل النهائي بإضافة حقل public_id صريح لكل فرضية في
hypotheses_catalog، وRuleEngine يتحقق (Fail Fast وقت الإنشاء) من أن كل
public_id موجود، يطابق pattern الـ schema، وفريد عبر كل الفرضيات.

## Bug رابع: additional_evidence_weight كانت غير متماثلة (Order-Dependent) — مُصحَّح
اكتُشف عند تشغيل CLI حقيقي (وليس demo_run.py) على incident حقيقي: جدول
corroboration_rules.additional_evidence_weight كان يحتوي فقط على أوزان
لـ CR002 وCR004 (القواعد المتوقَّعة كـ "دليل ثانٍ")، بافتراض ضمني غير
موثَّق أن الـ traceback (CR001/CR003) سيصل دائمًا أولًا في sources dict.
هذا الافتراض كان صحيحًا فقط لأن demo_run.py وfixtures الاختبارات كانت
تبني sources بترتيب {"traceback": ..., "git_diff": ...} دائمًا. لما CLI
الحقيقي جمع git_diff قبل traceback (لأن collect_sources بيضيف git_diff
أولًا)، الدليل الثاني (CR001) دوّر على وزنه في الجدول ولقاه غير موجود،
فأخذ 0.0 بدل 0.35 — والنتيجة طلعت score=0.55 بدل 0.90.

**الدرس المهم:** الاختبارات الآلية (45 وقتها) كلها كانت خضراء لأنها
بنيت على نفس الترتيب الثابت اللي عنده الافتراض الخفي — الـ bug ماكانش
هيتكشف إلا بتشغيل حقيقي بترتيب مختلف. هذا مثال مباشر على قيمة "شغّل
المشروع الحقيقي" وليس فقط "الاختبارات خضراء".

**التصحيح:** الجدول بقى متماثلًا (Symmetric) — CR001 وCR002 (ولاحقًا
CR003 وCR004) لهم نفس وزن التعزيز 0.35 بصرف النظر عن مين وصل أولًا.
أُضيف اختبار regression صريح `test_corroboration_score_is_order_independent`
يشغّل نفس السيناريو بترتيبين مختلفين للمصادر ويتأكد من تطابق النتيجة.

## قرار: نطاق Collectors محدود لـ Git محلي + ملفات محلية فقط
`GitCollector` بيشغّل `git` فعليًا عبر subprocess على repository محلي
على القرص، و`FileCollector` بيقرأ ملفات نصية حقيقية. **لا يوجد اتصال حي
بـ GitHub Actions API، Docker daemon، أو Kubernetes** — دول محتاجين
مصادقة (auth) واتصال شبكة غير متوفرين في بيئة التنفيذ الحالية. هذا خط
فاصل واضح ومقصود لهذا الإصدار، وليس نسيانًا:
- GitHub Actions API integration: يحتاج access token + استدعاء REST/GraphQL
  API لجلب logs الحقيقية من workflow run معين.
- Docker daemon query: يحتاج الاتصال بـ `/var/run/docker.sock` أو Docker API
  لجلب container logs مباشرة بدل ملف محفوظ يدويًا.
- Kubernetes: يحتاج kubeconfig + client (kubectl أو Python client) لجلب
  pod logs وevents.

كل واحد من دول Collector إضافي جديد بس (بنفس نمط GitCollector/FileCollector)
— لا يحتاج أي تعديل في pipeline.py أو أي طبقة تانية، لأن كل الـ Collectors
بترجع نفس الشكل: `{"source_name": "raw text"}`.
