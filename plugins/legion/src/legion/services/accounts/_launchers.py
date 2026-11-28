# An account's program, found from settings and the tool descriptor.

import os
import shutil
from pathlib import Path
from typing import Optional

from legion.core._home import (
    full_path,
    home_path,
    resolve_directory,
    same_path,
)
from legion.core._settings_models import Agent, ToolDescriptor


class LauncherError(Exception):
    pass


# shutil.which spells the extension as PATHEXT does (codex.CMD); the folder entry has the file's own spelling.
def _on_disk(path: Path) -> Path:
    for entry in path.parent.iterdir():
        if os.path.normcase(entry.name) == os.path.normcase(path.name):
            return entry
    return path


# A command on PATH.
def find_command(name: str) -> Optional[Path]:
    found = shutil.which(name)
    return _on_disk(Path(found).absolute()) if found else None


def stock_command(tool: ToolDescriptor) -> Optional[Path]:
    return find_command(tool.command)


def is_stock(command: Path, tool: ToolDescriptor) -> bool:
    stock = stock_command(tool)
    return stock is not None and same_path(resolve_directory(command), resolve_directory(stock))


def registered_command(path: str, home: Path) -> Optional[Path]:
    command = full_path(home_path(path, home))
    return command if command.is_file() else None


def command_prefix(program: Path) -> tuple[Path, list[str]]:
    if program.suffix.lower() == ".ps1":
        return Path("pwsh"), ["-NoProfile", "-File", str(program)]
    return program, []


def target(agent: Agent, tool: ToolDescriptor, home: Path) -> Path:
    if agent.command is not None:
        command = registered_command(agent.command, home)
        if command is not None:
            return command
        raise LauncherError(f"target needed: {agent.id}; ask the owner for the registered executable")
    stock = stock_command(tool)
    if stock is None:
        raise LauncherError(f"{tool.id} is not installed; run legion tool install --tool {tool.id}")
    return stock
