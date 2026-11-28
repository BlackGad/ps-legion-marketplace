# Releasing a repository: remove what repository setup added outside .legion/, then .legion/ itself.

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from legion.core._run import run_command
from legion.core._text import decode_text
from legion.services.repository._lease import other_holder
from legion.services.repository._repository import (
    RepositoryError,
    _code_root,
    _git,
    _read,
    _reference,
    _write,
)

SHARED_IGNORE_LINES = (".legion/temp/", ".legion/docs/")
EXCLUDE_LINE = ".legion/"
RULES_FILES = ("AGENTS.md", "CLAUDE.md")


# The file text without the rules section as setup appended it; None when the file does not hold it exactly.
def _without_rules(path: Path) -> str | None:
    text = decode_text(path.read_bytes())[0].replace("\r\n", "\n")
    block = _reference("rules-file-lines.md").replace("\r\n", "\n").rstrip() + "\n"
    start = text.find(block)
    if start < 0:
        return None
    end = start + len(block)
    if text[:start].endswith("\n"):
        start -= 1
    return text[:start] + text[end:]


def _without_lines(path: Path, lines: tuple[str, ...]) -> str:
    return "".join(line for line in _read(path).splitlines(keepends=True) if line.rstrip("\r\n") not in lines)


def _lane_worktrees(repo: Path) -> list[Path]:
    listing = _git(repo, "worktree", "list", "--porcelain", failure="Cannot list the Git worktrees")
    lanes = []
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
            if path.is_relative_to(repo / ".legion" / "temp"):
                lanes.append(path)
    return lanes


def _remove_worktree(repo: Path, lane: Path) -> None:
    result = run_command("git", ["-C", str(repo), "worktree", "remove", "--force", "--force", str(lane)])
    if result.exit_code:
        raise RepositoryError(f"Cannot remove the lane worktree {lane}")


def _prune(repo: Path) -> None:
    _git(repo, "worktree", "prune", failure="Cannot prune the Git worktrees")


# A read-only file (Git objects on Windows) blocks the delete until it is made writable.
def _force_remove(function: Callable[[str], object], path: str, _error: BaseException) -> None:
    Path(path).chmod(stat.S_IWRITE)
    function(path)


def _steps(repo: Path, private: bool) -> list[tuple[str, Callable[[], None]]]:
    steps: list[tuple[str, Callable[[], None]]] = []
    if private:
        name = _git(repo, "rev-parse", "--git-path", "info/exclude", failure="Cannot find the repository exclude file").strip()
        exclude = Path(os.path.normpath(repo / name))
        if EXCLUDE_LINE in _read(exclude).splitlines():
            steps.append((f"Remove the line {EXCLUDE_LINE} from {exclude}", lambda: _write(exclude, _without_lines(exclude, (EXCLUDE_LINE,)))))
    else:
        ignore = repo / ".gitignore"
        present = tuple(line for line in SHARED_IGNORE_LINES if line in _read(ignore).splitlines())
        if present:
            steps.append((f"Remove the lines {', '.join(present)} from .gitignore", lambda: _write(ignore, _without_lines(ignore, present))))
        for name in RULES_FILES:
            cleaned = _without_rules(repo / name) if (repo / name).is_file() else None
            if cleaned is not None:
                steps.append((f"Remove the legion section from {name}", lambda name=name, cleaned=cleaned: _write(repo / name, cleaned)))
    for lane in _lane_worktrees(repo):
        steps.append((f"Remove the lane worktree {lane}", lambda lane=lane: _remove_worktree(repo, lane)))
    steps.append(("Run git worktree prune", lambda: _prune(repo)))
    steps.append(("Remove .legion/", lambda: shutil.rmtree(repo / ".legion", onexc=_force_remove)))
    return steps


def dismiss(repo: Path, yes: bool, ask: Callable[[], bool], say: Callable[[str], None]) -> None:
    if os.environ.get("LEGION_ROLE", "").lower() == "member":
        raise RepositoryError("a member session cannot dismiss the repository; ask the overseer")
    repo = _code_root(repo)
    legion = repo / ".legion"
    if not legion.exists() and not legion.is_symlink():
        say("skip: legion in this repository (already dismissed)")
        return
    if legion.is_symlink() or legion.is_junction() or not legion.is_dir():
        raise RepositoryError(".legion is a link or not a folder; remove it by hand")
    holder = other_holder(repo)
    if holder:
        raise RepositoryError(f"the overseer lease is held by {holder}; end that session first")

    private = (legion / ".git").exists()
    steps = _steps(repo, private)
    for description, _ in steps:
        say(f"Plan: {description}")
    if private:
        say("Warning: this repository's legion history in .legion/ is deleted with it.")
    else:
        say("The file changes stay uncommitted for you to review.")
    if not yes and not ask():
        say("Nothing changed.")
        return
    for description, run in steps:
        say(description)
        run()
    say("Dismissed: legion is released from this repository.")
