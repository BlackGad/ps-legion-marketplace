# Fakes for the uv tool tests: a plugin source folder and a recording uv.

import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from legion.services.installation import _uv_tool
from legion.core._run import RunResult

if sys.platform == "win32":
    import _winapi

VERSION = "1.0.0"


def make_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def make_source(root: Path, version: str = VERSION) -> Path:
    source = root / "source"
    (source / "src").mkdir(parents=True, exist_ok=True)
    (source / "plugin.json").write_text(json.dumps({"name": "legion", "version": version}), encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (source / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    return source


class FakeUv:
    # A recording uv. `calls` holds the arguments of each uv command run in this process; `detached` holds each
    # command handed to the helper that runs after exit, with the paths it deletes. The tool state is `installed`.
    def __init__(self, monkeypatch: pytest.MonkeyPatch, bin_folder: Path) -> None:
        self.calls: list[list[str]] = []
        self.detached: list[tuple[list[str], list[Path]]] = []
        self.logs: list[Path | None] = []
        self.purges: list[list[Path]] = []
        self.installed: str | None = None
        self.shell_updated = False
        self.fail: dict[str, int] = {}
        self.bin = bin_folder
        # Where `uv tool dir` says the tools live.
        self.tools = bin_folder.parent / "uv tools"
        monkeypatch.setattr(_uv_tool, "find_uv", lambda: "uv")
        monkeypatch.setattr(_uv_tool, "run_command", self.run)
        monkeypatch.setattr(_uv_tool, "run_foreground", self.run_foreground)
        monkeypatch.setattr("legion.services.installation._constraints.run_command", self.run)
        monkeypatch.setattr(_uv_tool._handover, "start", self.hand_over)

    def hand_over(self, command: list[str], cleanup: list[Path], *, purge: list[Path], log: Path | None) -> None:
        self.detached.append((command, cleanup))
        if purge:
            self.purges.append(purge)
        self.logs.append(log)

    def installs(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["tool", "install"]]

    def run_foreground(self, command: str, arguments: list[str]) -> int:
        return self.run(command, arguments).exit_code

    def run(self, command: str, arguments: list[str], timeout: float | None = None, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> RunResult:
        assert command == "uv"
        self.calls.append(list(arguments))
        name = " ".join(arguments[:2])
        output = ""
        if name in self.fail:
            return RunResult(exit_code=self.fail[name], output="", error=f"{name} failed", timed_out=False)
        if name == "tool list" and self.installed:
            output = f"legion v{self.installed}\n- legion\n"
        elif arguments == ["tool", "dir"]:
            output = f"{self.tools}\n"
        elif arguments == ["tool", "dir", "--bin"]:
            output = f"{self.bin}\n"
        elif name == "tool update-shell":
            self.shell_updated = True
        elif arguments[0] == "export":
            output = "mcp==1.0.0\n"
        elif name == "tool install":
            root = Path(arguments[2].removesuffix("[mcp]"))
            self.installed = json.loads((root / "plugin.json").read_text(encoding="utf-8"))["version"]
            program = self.bin / ("legion.exe" if sys.platform == "win32" else "legion")
            program.parent.mkdir(parents=True, exist_ok=True)
            program.touch()
            if sys.platform != "win32":
                program.chmod(0o755)
        return RunResult(exit_code=0, output=output, error="", timed_out=False)
