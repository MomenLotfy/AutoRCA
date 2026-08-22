"""
demo_run.py
-----------------------------------------------------------------------------
سكريبت تعليمي — شغّله بـ: python3 demo_run.py
بيوريك كل طبقة من طبقات AutoRCA وهي شغالة فعليًا، مع شرح إيه معنى كل
نتيجة، على 4 سيناريوهات مختلفة عشان توضح كل أنواع المخرجات الممكنة.
-----------------------------------------------------------------------------
"""

import json
from pipeline import AnalysisPipeline, PipelineInput
from rca_request.rca_request_builder import RCARequestBuilder, RepositoryContext

SEP = "=" * 78


def print_header(title):
    print("\n" + SEP)
    print(f"  {title}")
    print(SEP)


def show_pipeline_result(result):
    print(f"\n[1] Observations المستخلصة: {len(result.observations)}")
    for o in result.observations:
        print(f"    - {o.id}: kind={o.kind}  source={o.source}  data={o.data}")

    print(f"\n[2] Evidence المصنّفة: {len(result.evidence_list)}")
    for e in result.evidence_list:
        print(f"    - {e['id']}: failure_type={e['failure_type_id']}  "
              f"rule={e['classification_rule_id']}  data={e['data']}")

    print(f"\n[3] Hypotheses (كل الفرضيات، مش بس الفايزة): {len(result.hypotheses)}")
    for h in result.hypotheses:
        print(f"    - {h.id}: {h.label}  score={h.score}  status={h.status}")

    print(f"\n[4] القرار النهائي (result.selected):")
    if result.selected:
        s = result.selected
        print(f"    ✅ اتختارت فرضية: {s.id} ({s.label})  score={s.score}")
    else:
        print(f"    ⚠️  مفيش فرضية اتختارت (status='selected' لحد واحدة منهم)")
        print(f"        السبب المحتمل: مفيش دليل كفاية، أو فيه تعارض بين فرضيتين متقاربتين")


def main():
    pipeline = AnalysisPipeline.from_config_files(
        rules_config_path="rules/rules.config.json",
        taxonomy_path="taxonomy/taxonomy.yaml",
    )
    builder = RCARequestBuilder(
        rule_engine=pipeline._rule_engine,
        scoring_engine=pipeline._scoring_engine,
        taxonomy_index=pipeline._taxonomy_index,
        rules_config=pipeline._rules_config,
    )

    # -------------------------------------------------------------------
    # سيناريو 1: نجاح كامل — دليلين مستقلين بيأكدوا نفس السبب (missing_env)
    # -------------------------------------------------------------------
    print_header("سيناريو 1: Missing Env — دليلين مستقلين (النتيجة المتوقعة: selected, score=0.90)")

    traceback_text = (
        "Traceback (most recent call last):\n"
        '  File "app.py", line 42, in <module>\n'
        "    port = os.environ['PORT']\n"
        "KeyError: 'PORT'\n"
    )
    git_diff_text = (
        "diff --git a/.env b/.env\n"
        "index abc1234..def5678 100644\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@ -1,3 +1,2 @@\n"
        " DEBUG=true\n"
        "-PORT=8000\n"
        " SECRET_KEY=xyz\n"
    )

    result1 = pipeline.run(PipelineInput(
        analysis_id="AR20260804-001",
        sources={"traceback": traceback_text, "git_diff": git_diff_text},
    ))
    show_pipeline_result(result1)

    if result1.selected:
        rca_request = builder.build(
            analysis_id=result1.analysis_id,
            selected=result1.selected,
            all_hypotheses=result1.hypotheses,
            evidence_list=result1.evidence_list,
            repository_context=RepositoryContext(
                full_name="momen/nexvault", branch="main",
                commit_sha="abc1234", environment="production",
            ),
            diff_source=git_diff_text,
            log_source=traceback_text,
        )
        print(f"\n[5] RCARequest جاهز للـ LLM (هيتبعتله ده بس، مش اللوج الخام):")
        print(json.dumps(rca_request.to_dict(), indent=2, ensure_ascii=False))

    # -------------------------------------------------------------------
    # سيناريو 2: دليل واحد بس (بدون git diff) — لسه بيتاخد قرار
    # -------------------------------------------------------------------
    print_header("سيناريو 2: Missing Env — دليل واحد بس (النتيجة المتوقعة: selected, score=0.55)")
    result2 = pipeline.run(PipelineInput(
        analysis_id="AR20260804-002",
        sources={"traceback": traceback_text},
    ))
    show_pipeline_result(result2)

    # -------------------------------------------------------------------
    # سيناريو 3: لوج نضيف تمامًا — مفيش دليل خالص
    # -------------------------------------------------------------------
    print_header("سيناريو 3: لوج CI ناجح (النتيجة المتوقعة: صفر observations, صفر hypotheses)")
    clean_log = "Running unit tests...\ntest_health_check ... ok\nAll 12 tests passed.\n"
    result3 = pipeline.run(PipelineInput(
        analysis_id="AR20260804-003",
        sources={"ci_log": clean_log},
    ))
    show_pipeline_result(result3)

    # -------------------------------------------------------------------
    # سيناريو 4: Port Conflict — نوع تاني تمامًا، دليل واحد بس متاح له
    # -------------------------------------------------------------------
    print_header("سيناريو 4: Port Conflict (النتيجة المتوقعة: selected, score=0.60)")
    docker_output = (
        "> app@1.0.0 start\n> node server.js\n\n"
        "Error: listen EADDRINUSE: address already in use :::8000\n"
    )
    result4 = pipeline.run(PipelineInput(
        analysis_id="AR20260804-004",
        sources={"docker_output": docker_output},
    ))
    show_pipeline_result(result4)

    print("\n" + SEP)
    print("  خلصنا. شوف الشرح تحت كل سيناريو فوق عشان تفهم معنى كل نتيجة.")
    print(SEP)


if __name__ == "__main__":
    main()
