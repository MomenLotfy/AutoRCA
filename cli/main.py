"""
cli/main.py
-----------------------------------------------------------------------------
واجهة تشغيل حقيقية فوق AnalysisPipeline — الاستخدام المتوقع:

    python3 -m cli.main analyze \
        --repo /path/to/real/project \
        --environment production \
        --commit <sha> \
        --traceback /path/to/real/traceback.txt \
        [--docker-log /path/to/docker_output.txt]

الفرق الجوهري عن demo_run.py: البيانات هنا بتيجي من مصادر حقيقية على
القرص (git diff فعلي من commit حقيقي، ملف traceback حقيقي) — مش نصوص
مكتوبة يدويًا داخل الكود. لو مفيش أي مصدر اتحدد، الأداة بترفض تشتغل
(Fail Fast) بدل ما تنتج نتيجة فاضية بصمت.

نطاق محدود بوضوح: هذا CLI بيتعامل مع repository محلي وملفات محلية فقط.
الاتصال الحي بـ GitHub Actions API / Docker daemon / Kubernetes غير
متاح هنا ومسجّل في docs/TECHNICAL_DEBT.md كخطوة تالية.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from collectors.file_collector import FileCollector, FileCollectorError
from collectors.git_collector import GitCollector, GitCollectorError
from pipeline import AnalysisPipeline, PipelineInput
from rca_request.rca_request_builder import RCARequestBuilder, RepositoryContext
from reporting.incident_report_renderer import IncidentReportRenderer
from llm import LLMAnalysisService, LLMClientError, OpenAICompatibleLLMClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES_CONFIG = PROJECT_ROOT / "rules" / "rules.config.json"
DEFAULT_TAXONOMY = PROJECT_ROOT / "taxonomy" / "taxonomy.yaml"


class CLIError(RuntimeError):
    pass


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autorca")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze a real incident from local sources.")
    analyze.add_argument("--repo", required=True, help="Path to a local git repository.")
    analyze.add_argument("--environment", required=True, choices=["local", "staging", "production"])
    analyze.add_argument("--commit", default=None, help="Commit SHA to diff (defaults to last commit).")
    analyze.add_argument("--traceback", default=None, help="Path to a real traceback/log file.")
    analyze.add_argument("--docker-log", default=None, help="Path to a real docker output file.")
    analyze.add_argument("--ci-log", default=None, help="Path to a real CI log file.")
    analyze.add_argument("--full-name", default=None, help="Repository full name, e.g. org/repo.")
    analyze.add_argument("--no-diff", action="store_true", help="Skip collecting a git diff entirely.")
    analyze.add_argument(
        "--llm",
        action="store_true",
        help="Generate and validate a real LLM explanation; requires LLM environment variables.",
    )
    analyze.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON RCA result including timeline, correlation, graph, fingerprint, remediation.",
    )

    return parser


def collect_sources(args: argparse.Namespace, git_collector: GitCollector) -> dict:
    sources: dict = {}

    if not args.no_diff:
        try:
            sources["git_diff"] = git_collector.collect_diff(commit=args.commit)
        except GitCollectorError as e:
            print(f"[warn] git diff collection skipped: {e}", file=sys.stderr)

    if args.traceback:
        sources["traceback"] = FileCollector.collect(args.traceback)
    if args.docker_log:
        sources["docker_output"] = FileCollector.collect(args.docker_log)
    if args.ci_log:
        sources["ci_log"] = FileCollector.collect(args.ci_log)

    if not sources:
        raise CLIError(
            "No incident data collected. Provide at least one of --traceback, "
            "--docker-log, --ci-log, or allow git diff collection (do not pass --no-diff)."
        )

    return sources


def run_analyze(args: argparse.Namespace) -> int:
    try:
        git_collector = GitCollector(args.repo)
    except GitCollectorError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    try:
        sources = collect_sources(args, git_collector)
    except (CLIError, FileCollectorError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    try:
        commit_sha = args.commit or git_collector.resolve_commit_sha()
    except GitCollectorError:
        commit_sha = None
    try:
        branch = git_collector.resolve_branch()
    except GitCollectorError:
        branch = "unknown"

    pipeline = AnalysisPipeline.from_config_files(
        rules_config_path=DEFAULT_RULES_CONFIG,
        taxonomy_path=DEFAULT_TAXONOMY,
    )
    analysis_id = f"AR{_timestamp_id()}"
    result = pipeline.run(PipelineInput(analysis_id=analysis_id, sources=sources))

    if args.llm:
        if result.selected is None:
            print("[error] No root cause identified; LLM was not called.", file=sys.stderr)
            return 2
        try:
            builder = RCARequestBuilder(
                rule_engine=pipeline._rule_engine,
                scoring_engine=pipeline._scoring_engine,
                taxonomy_index=pipeline._taxonomy_index,
                rules_config=pipeline._rules_config,
            )
            request = builder.build(
                analysis_id=result.analysis_id,
                selected=result.selected,
                all_hypotheses=result.hypotheses,
                evidence_list=result.evidence_list,
                repository_context=RepositoryContext(
                    full_name=args.full_name or Path(args.repo).name,
                    branch=branch,
                    commit_sha=commit_sha or "unknown",
                    environment=args.environment,
                ),
                diff_source=sources.get("git_diff"),
                log_source=(
                    sources.get("traceback")
                    or sources.get("ci_log")
                    or sources.get("docker_output")
                ),
            )
            final_rca = LLMAnalysisService(
                OpenAICompatibleLLMClient()
            ).generate_validated(request)
        except LLMClientError as e:
            print(f"[error] {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"[error] LLM result rejected by FinalRCAValidator: {e}", file=sys.stderr)
            return 1
        print(json.dumps(final_rca, ensure_ascii=False, indent=2))
        return 0

    confidence = None
    if result.selected:
        confidence = pipeline.compute_confidence(result.selected.score)

    if args.json:
        payload = {
            "analysis_id": result.analysis_id,
            "commit_sha": commit_sha,
            "branch": branch,
            "environment": args.environment,
            "selected_hypothesis": result.selected.to_dict() if result.selected else None,
            "confidence": confidence,
            "observations": [o.to_dict() for o in result.observations],
            "evidence": result.evidence_list,
            "all_hypotheses": [h.to_dict() for h in result.hypotheses],
            "timeline": result.timeline.to_dict() if result.timeline else None,
            "correlation": result.correlation.to_dict() if result.correlation else None,
            "graph": result.graph.to_dict() if result.graph else None,
            "fingerprint": result.fingerprint.to_dict() if result.fingerprint else None,
            "remediation": result.remediation.to_dict() if result.remediation else None,
            "hypothesis_assessment": result.hypothesis_assessment.to_dict() if result.hypothesis_assessment else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if result.selected else 2

    renderer = IncidentReportRenderer(fix_hints=pipeline._rules_config.fix_hints)
    report = renderer.render(result, commit_sha=commit_sha, confidence=confidence)

    print(report)
    return 0 if result.selected else 2


def _timestamp_id() -> str:
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%d-%H%M%S")


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "analyze":
        return run_analyze(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
