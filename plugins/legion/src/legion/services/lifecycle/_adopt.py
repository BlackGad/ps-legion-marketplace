# Adopt an existing tool folder: copy it into the account's own folder and make the copy work there.
# The source stays untouched. Only the files the tool's descriptor lists are opened for rewriting;
# credentials and history are copied as they are.

import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from legion.core._settings_models import ToolDescriptor

IGNORE_CASE = os.name == "nt"


class AdoptError(ValueError):
    pass


# Every spelling of the old folder (plain, JSON-escaped, forward slashes), never a longer name such as .claude-account2.
# A \?\ prefix stays where it is, so the folder after it is the only part that changes.
def _pattern(old: Path) -> re.Pattern[str]:
    plain = str(old)
    spellings = dict.fromkeys([plain.replace("\\", "\\\\"), plain, old.as_posix()])
    alternatives = "|".join(re.escape(spelling) for spelling in spellings)
    return re.compile(rf"(?<![\w.-])(?:{alternatives})(?![\w.-])", re.IGNORECASE if IGNORE_CASE else 0)


def rewrite_text(text: str, old: Path, new: Path) -> tuple[str, int]:
    def replacement(match: re.Match[str]) -> str:
        found = match.group(0)
        if "\\\\" in found:
            return str(new).replace("\\", "\\\\")
        return new.as_posix() if "/" in found else str(new)

    return _pattern(old).subn(replacement, text)


def _rewrite_file(file: Path, old: Path, new: Path, say: Callable[[str], None]) -> None:
    text, count = rewrite_text(file.read_bytes().decode("utf-8"), old, new)
    if count:
        file.write_bytes(text.encode("utf-8"))
        say(f"rewrote {count} path(s) in {file}")


# A target that already holds files is refused; a copy that fails leaves no half-made folder behind.
def adopt(source: Path, target: Path, tool: ToolDescriptor, say: Callable[[str], None]) -> None:
    if target.exists() and any(target.iterdir()):
        raise AdoptError(f"{target} already has files; adopt copies a folder into an empty one")
    if target.is_relative_to(source):
        raise AdoptError(f"{target} is inside {source}; choose a folder outside the new account")
    say(f"copying {source} into {target}")
    try:
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        for name in tool.plugin.adopt_rewrite or []:
            file = target / name
            if file.is_file():
                _rewrite_file(file, source, target, say)
    except (OSError, ValueError):
        shutil.rmtree(target, ignore_errors=True)
        raise
