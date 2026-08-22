"""
tests/test_collectors_and_cli.py
-----------------------------------------------------------------------------
يبني git repository حقيقي مؤقت (tmp_path) بـ commit فعلي، ويشغّل
GitCollector/FileCollector/CLI عليه فعليًا — بدون أي بيانات مصطنعة.
هذا يثبت أن AutoRCA يعمل على مصادر حقيقية على القرص، وليس فقط على نصوص
مكتوبة يدويًا داخل fixtures.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from collectors.file_collector import FileCollector, FileCollectorError
from collectors.git_collector import GitCollector, GitCollectorError
from cli.main import build_arg_parser, run_analyze


def _run_git(repo_path: Path, *args: str) -> None:
    result = subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture()
def real_git_repo(tmp_path) -> Path:
    """
    يبني repo حقيقي بـ commit أول (PORT موجود) وcommit ثانٍ حقيقي بيشيل
    PORT فعليًا عبر git، بدون أي محاكاة أو نص diff مكتوب يدويًا.
    """
    repo = tmp_path / "incident_repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@local")
    _run_git(repo, "config", "user.name", "Test")

    env_file = repo / ".env"
    env_file.write_text("DEBUG=true\nPORT=8000\nSECRET_KEY=xyz\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "Initial working deployment")

    env_file.write_text("DEBUG=true\nSECRET_KEY=xyz\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "Remove PORT by mistake")

    return repo


def test_git_collector_returns_real_diff_for_last_commit(real_git_repo):
    collector = GitCollector(str(real_git_repo))
    diff = collector.collect_diff()
    assert "-PORT=8000" in diff
    assert "diff --git" in diff


def test_git_collector_returns_real_diff_for_specific_commit(real_git_repo):
    collector = GitCollector(str(real_git_repo))
    commit_sha = collector.resolve_commit_sha("HEAD")
    diff = collector.collect_diff(commit=commit_sha)
    assert "-PORT=8000" in diff


def test_git_collector_rejects_invalid_repo_path(tmp_path):
    with pytest.raises(GitCollectorError, match="غير موجود"):
        GitCollector(str(tmp_path / "does-not-exist"))


def test_git_collector_rejects_non_git_directory(tmp_path):
    plain_dir = tmp_path / "not_a_repo"
    plain_dir.mkdir()
    with pytest.raises(GitCollectorError, match="ليس git repository"):
        GitCollector(str(plain_dir))


def test_git_collector_resolve_commit_and_branch(real_git_repo):
    collector = GitCollector(str(real_git_repo))
    sha = collector.resolve_commit_sha()
    assert len(sha) == 40  # SHA-1 كامل حقيقي من git، مش placeholder
    branch = collector.resolve_branch()
    assert branch  # اسم فرع حقيقي (master أو main حسب إعداد git المحلي)


def test_file_collector_reads_real_file(tmp_path):
    f = tmp_path / "traceback.txt"
    f.write_text("KeyError: 'PORT'\n", encoding="utf-8")
    content = FileCollector.collect(str(f))
    assert "KeyError" in content


def test_file_collector_rejects_missing_file(tmp_path):
    with pytest.raises(FileCollectorError, match="غير موجود"):
        FileCollector.collect(str(tmp_path / "nope.txt"))


def test_file_collector_rejects_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    with pytest.raises(FileCollectorError, match="فاضي"):
        FileCollector.collect(str(f))


# =============================================================================
# CLI end-to-end — على بيانات حقيقية بالكامل (git repo حقيقي + traceback حقيقي)
# =============================================================================


def test_cli_analyze_end_to_end_on_real_repo(real_git_repo, tmp_path, capsys):
    traceback_file = tmp_path / "real_traceback.txt"
    traceback_file.write_text("KeyError: 'PORT'\n", encoding="utf-8")

    parser = build_arg_parser()
    args = parser.parse_args([
        "analyze",
        "--repo", str(real_git_repo),
        "--environment", "production",
        "--traceback", str(traceback_file),
    ])

    exit_code = run_analyze(args)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Missing environment variable" in captured.out
    assert "90%" in captured.out
    assert "Git diff" in captured.out
    assert "Application traceback" in captured.out
    assert "Related Change" in captured.out
    assert "Deterministic placeholder" in captured.out


def test_cli_analyze_fails_fast_with_no_sources(real_git_repo, capsys):
    parser = build_arg_parser()
    args = parser.parse_args([
        "analyze",
        "--repo", str(real_git_repo),
        "--environment", "production",
        "--no-diff",
    ])

    exit_code = run_analyze(args)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No incident data collected" in captured.err


def test_cli_analyze_reports_no_root_cause_for_unrelated_traceback(real_git_repo, tmp_path, capsys):
    traceback_file = tmp_path / "unrelated.txt"
    traceback_file.write_text("Some unrelated log line with no known pattern.\n", encoding="utf-8")

    parser = build_arg_parser()
    args = parser.parse_args([
        "analyze",
        "--repo", str(real_git_repo),
        "--environment", "production",
        "--traceback", str(traceback_file),
        "--no-diff",
    ])

    exit_code = run_analyze(args)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "No root cause identified" in captured.out
