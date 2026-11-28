import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from click.testing import CliRunner, Result

from legion.cli import main
from legion.core._run import run_command
from legion.services.repository import _lease

PLAN_LAST = "Plan: Remove .legion/"
DONE = "Dismissed: legion is released from this repository."
SKIP = "skip: legion in this repository (already dismissed)\n"


def git(repo: Path, *args: str) -> str:
    result = run_command("git", ["--literal-pathspecs", "-C", str(repo), *args])
    assert result.exit_code == 0, result.error
    return result.output.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "code repo"
    path.mkdir()
    git(path, "init", "--quiet")
    git(path, "config", "user.name", "Fixture Owner")
    git(path, "config", "user.email", "fixture@example.invalid")
    (path / "AGENTS.md").write_text("owner rules\n", encoding="utf-8")
    (path / ".gitignore").write_text("existing/\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "--quiet", "-m", "Fixture")
    monkeypatch.chdir(path)
    monkeypatch.delenv("LEGION_ROLE", raising=False)
    return path


def legion(*arguments: str, text: str = "") -> Result:
    return CliRunner().invoke(main, list(arguments), input=text)


def set_up(mode: Literal["private", "shared"]) -> None:
    assert legion("repository", "setup", "--mode", mode).exit_code == 0


def exclude_text(repo: Path) -> str:
    return (repo / ".git/info/exclude").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["private", "shared"])
def test_dismiss_restores_the_repository(repo: Path, mode: Literal["private", "shared"]) -> None:
    exclude_before = exclude_text(repo)
    set_up(mode)
    result = legion("dismiss", "--yes")
    lines = result.output.splitlines()
    assert result.exit_code == 0
    assert lines[-1] == DONE
    assert PLAN_LAST in lines
    assert ("Warning: this repository's legion history in .legion/ is deleted with it." in lines) == (mode == "private")
    assert not (repo / ".legion").exists()
    assert exclude_text(repo) == exclude_before
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "existing/\n"
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "owner rules\n"
    assert git(repo, "worktree", "list").count("\n") == 0


def test_shared_changes_stay_uncommitted(repo: Path) -> None:
    set_up("shared")
    head = git(repo, "rev-parse", "HEAD")
    assert legion("dismiss", "--yes").exit_code == 0
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain")


@pytest.mark.parametrize(("answer", "removed"), [("y\n", True), ("n\n", False), ("", False)])
def test_question_and_closed_input(repo: Path, answer: str, removed: bool) -> None:
    set_up("private")
    result = legion("dismiss", text=answer)
    assert result.exit_code == 0
    assert "Apply? [y/N]" in result.output
    assert (not (repo / ".legion").exists()) == removed
    assert (DONE in result.output) == removed


@pytest.mark.usefixtures("repo")
def test_repeat_skips() -> None:
    set_up("shared")
    assert legion("dismiss", "--yes").exit_code == 0
    result = legion("dismiss", "--yes")
    assert (result.exit_code, result.output) == (0, SKIP)


@pytest.mark.usefixtures("repo")
def test_repository_without_legion_skips() -> None:
    result = legion("dismiss", "--yes")
    assert (result.exit_code, result.output) == (0, SKIP)


def test_owner_content_is_kept(repo: Path) -> None:
    git(repo, "config", "core.autocrlf", "false")
    (repo / "AGENTS.md").write_bytes(b"owner rules\r\n")
    git(repo, "add", "--renormalize", ".")
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "crlf")
    set_up("shared")
    rules = repo / "AGENTS.md"
    rules.write_bytes(rules.read_bytes() + b"owner tail\r\n")
    assert legion("dismiss", "--yes").exit_code == 0
    assert rules.read_bytes() == b"owner rules\r\nowner tail\r\n"


def test_member_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_up("private")
    monkeypatch.setenv("LEGION_ROLE", "member")
    result = legion("dismiss", "--yes")
    assert result.exit_code == 1
    assert (repo / ".legion").exists()


def test_live_lease_of_another_session_is_refused(repo: Path) -> None:
    set_up("private")
    # A separate process: the test's parent can be the caller's own session, which dismiss may release.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _lease.take(repo, "codex", "codex-a", process_id=other.pid)
        result = legion("dismiss", "--yes")
    finally:
        other.kill()
        other.wait()
    assert result.exit_code == 1
    assert (repo / ".legion").exists()


def test_lane_worktrees_are_removed(repo: Path) -> None:
    set_up("private")
    lane = repo / ".legion/temp/t1"
    git(repo, "worktree", "add", "--quiet", "-b", "lane-t1", str(lane))
    result = legion("dismiss", "--yes")
    assert result.exit_code == 0
    assert not (repo / ".legion").exists()
    assert "t1" not in git(repo, "worktree", "list")
