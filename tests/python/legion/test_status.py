import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import pytest
from cli_runner import run
from legion.core._run import RunResult

from legion.services.repository import _lease
from legion.services.lifecycle import _targets
from legion.services.status import _status
from legion.core import _process, _validate
from legion.core._settings_models import ChannelDescriptor, PersonalisationSettings, ToolDescriptor
from legion.services.status import status
from legion.services import status as status_service

pytest_plugins = ["status_parity"]

REPO = Path(__file__).resolve().parents[3]
REQUIRED = ("configuration/project.json", "configuration/workflow.md", "scripts/index.md")
WARNING = "legion: channel my-telegram is bound to this account but not connected in this session; restart with legion to connect it."


def test_status_report_matches_status_lines(parity_home: tuple[Path, Path]) -> None:
    repo, home = parity_home
    before = snapshot(home), snapshot(repo)
    report = status(repo, home)
    assert (snapshot(home), snapshot(repo)) == before
    assert _status.status_lines(report) == [
        "accounts",
        "  claude-a  claude  ~/.claude  legion missing",
        "  codex-a  codex  ~/.codex  legion missing",
        *(["outlanders", *(f"  {row.tool}  {row.config_dir}  legion missing" for row in report.outlanders)] if report.outlanders else []),
        "channels main (telegram) -> claude-a",
        "knowledge: off (not asked)",
        "repo Shared   lease: free   handoff: yes",
    ]
    assert report.repository.mode == "Shared"
    assert report.repository.lease == "free"


@pytest.mark.parametrize("broken", ["invalid", "unreadable"])
def test_status_report_broken_settings_names_file_without_content(tmp_path: Path, broken: str) -> None:
    file = tmp_path / ".legion/configuration/personalisation.json"
    if broken == "invalid":
        write(file, '{"private-sentinel":')
    else:
        file.mkdir(parents=True)
    report = status(tmp_path, tmp_path)
    assert report.settings.state == broken
    assert report.settings.path == str(file)
    assert "private-sentinel" not in report.model_dump_json()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def settings(home: Path) -> PersonalisationSettings:
    data = {
        "schemaVersion": 1,
        "agents": [{"id": "claude-a", "tool": "claude", "configDir": "~/.claude"}, {"id": "codex-a", "tool": "codex", "configDir": "~/.codex-work"}],
        "channels": [{"id": "my-telegram", "kind": "telegram", "target": "123", "accounts": ["claude-a"]}],
    }
    write(home / ".legion/configuration/personalisation.json", json.dumps(data))
    return PersonalisationSettings.model_validate(data)


def git(repo: Path, *words: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    result = subprocess.run([executable, "-C", str(repo), *words], stdin=subprocess.DEVNULL, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def scaffold(repo: Path) -> Path:
    legion = repo / ".legion"
    for file in REQUIRED:
        write(legion / file, "fixture")
    return legion


# Status output, settings, PATH, ownership, tracking, leases and read-only behavior.
def test_status_without_settings_is_native_and_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert run(["--home", str(tmp_path / "home"), "status"]) == 0
    assert capsys.readouterr().out.splitlines() == ["no legion settings yet. Run legion add <tool> to add an account.", "knowledge: off (not asked)", "repo none"]


@pytest.mark.parametrize("broken", ["invalid", "extra", "unreadable"])
def test_status_tells_broken_settings_from_missing_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], broken: str) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    home = tmp_path / "home"
    file = home / ".legion/configuration/personalisation.json"
    if broken == "invalid":
        write(file, '{"private-sentinel": ')
    elif broken == "extra":
        write(file, json.dumps({"schemaVersion": 1, "agents": [], "channels": [], "private-sentinel": "private-sentinel"}))
    else:
        (file / "private-sentinel").mkdir(parents=True)
    assert run(["--home", str(home), "status"]) == 0
    output = capsys.readouterr()
    problem = "cannot be read" if broken == "unreadable" else "are not valid"
    expected = f"legion settings {problem}: {file}; run legion setup"
    assert output.out.splitlines() == [expected, "knowledge: off (not asked)", "repo none"]
    assert "private-sentinel" not in output.out + output.err


def snapshot(home: Path) -> dict[Path, tuple[bytes, int]]:
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in home.rglob("*") if path.is_file()}


def test_configured_status_preserves_home_and_never_launches_a_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    configured = settings(home)
    configured.knowledge_enabled = True
    write(home / ".legion/configuration/personalisation.json", configured.model_dump_json(by_alias=True, exclude_none=True))
    write(home / ".claude/plugins/cache/ps-legion-marketplace/legion/0.6.0/plugin.json", '{"name":"legion","version":"0.6.0"}')
    write(home / ".claude/plugins/cache/llm-wiki/wiki/1.0.0/.claude-plugin/plugin.json", '{"name":"wiki","version":"1.0.0"}')
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("status started a process without a tracked repository")

    monkeypatch.setattr(status_service, "run_command", refuse)
    before = snapshot(home)
    assert run(["--home", str(home), "status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "accounts",
        "  claude-a  claude  ~/.claude  legion 0.6.0",
        "  codex-a  codex  ~/.codex-work  legion missing",
        "channels my-telegram (telegram) -> claude-a",
        "knowledge: on",
        "  knowledge hub missing: ~/.legion/knowledge",
        "  wiki plugin missing in: codex-a",
        "repo none",
    ]
    assert snapshot(home) == before


@pytest.mark.parametrize("words", [["--yes"], ["extra"], ["--repo", "elsewhere"]])
def test_status_rejects_unknown_arguments(words: list[str]) -> None:
    assert run(["status", *words]) == 2


def test_status_defaults_to_fenced_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path.home()
    settings(home)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _status.status_lines(status(tmp_path))[0] == "accounts"


def test_repository_requires_complete_tracked_scaffold_and_inner_private_history(tmp_path: Path) -> None:
    write(tmp_path / ".legion/temp/scratch", "fixture")
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo none"
    legion = scaffold(tmp_path)
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo none"
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "add", "--", *[f".legion/{file}" for file in REQUIRED])
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo Shared   lease: free   handoff: no"
    git(tmp_path, "rm", "--cached", "--quiet", "--", ".legion/scripts/index.md")
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo none"
    git(tmp_path, "add", "--", ".legion/scripts/index.md")
    for file in REQUIRED:
        path = legion / file
        path.unlink()
        assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo none"
        write(path, "fixture")
    git(legion, "init", "--quiet")
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo none"
    git(legion, "add", "--", *REQUIRED)
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo Private   lease: free   handoff: no"
    write(legion / "state/handoff.md", "handoff")
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo Private   lease: free   handoff: yes"
    assert _lease.take(tmp_path, "codex", "codex-a", os.getpid()) == "lease taken"
    before = snapshot(legion)
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo Private   lease: held   handoff: yes"
    assert snapshot(legion) == before
    assert _lease.release(tmp_path, os.getpid()) == "lease released"


def test_repository_git_probe_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scaffold(tmp_path)
    executable = shutil.which("git")
    assert executable is not None

    def probe(command: str, words: list[str], timeout: float) -> RunResult:
        assert command == executable
        assert words == ["-C", str(tmp_path), "ls-files", "--error-unmatch", "--", *[f".legion/{file}" for file in REQUIRED]]
        assert timeout == 5
        return RunResult(exit_code=0, output="", error="", timed_out=False)

    monkeypatch.setattr(status_service, "run_command", probe)
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo Shared   lease: free   handoff: no"


def test_repository_without_git_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scaffold(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _status.repository_line(status_service.repository_status(tmp_path)) == "repo none"


@pytest.mark.parametrize("tool_id", ["claude", "codex", "copilot"])
def test_installed_versions_use_descriptor_root_and_semantic_order(tmp_path: Path, tool_id: str) -> None:
    tool = next(tool for tool in _status.load_descriptors(ToolDescriptor, "tool.json") if tool.id == tool_id)
    assert tool.plugin.installed_root is not None
    for index, (name, version) in enumerate([("legion", "0.6.0"), ("legion", "0.10.0-beta.2"), ("legion", "0.10.0-beta.10"), ("legion", "0.10.0"), ("other", "99.0.0"), ("legion", "bad")]):
        write(tmp_path / tool.plugin.installed_root / str(index) / "plugin.json", json.dumps({"name": name, "version": version}))
    write(tmp_path / tool.plugin.installed_root / "broken/plugin.json", "{")
    assert _targets.installed_version(tmp_path, tool) == "0.10.0"


@pytest.mark.parametrize("registration", ["directory", "disabled-value", "not-enabled", "remote", "broken", "missing-manifest"])
def test_copilot_local_marketplace_takes_precedence(tmp_path: Path, registration: str) -> None:
    tool = next(tool for tool in _status.load_descriptors(ToolDescriptor, "tool.json") if tool.id == "copilot")
    assert tool.plugin.installed_root is not None
    write(tmp_path / tool.plugin.installed_root / "cached/plugin.json", '{"name":"legion","version":"9.0.0"}')
    marketplace = tmp_path / "local marketplace"
    write(
        marketplace / ".claude-plugin/marketplace.json",
        '{"name":"ps-legion-marketplace","plugins":[{"name":"other","source":{"source":"github","repo":"example/other"}},{"name":"legion","source":"./plugins/legion"}]}',
    )
    if registration != "missing-manifest":
        write(marketplace / "plugins/legion/plugin.json", '{"name":"legion","version":"0.7.0"}')
    enabled = {} if registration == "not-enabled" else {"legion@ps-legion-marketplace": registration != "disabled-value"}
    data = {
        "enabledPlugins": enabled,
        "extraKnownMarketplaces": {
            "another-marketplace": {"source": {"source": "github", "repo": "example/other"}},
            "ps-legion-marketplace": {"source": {"source": "remote" if registration == "remote" else "directory", "path": str(marketplace)}},
        },
    }
    write(tmp_path / "settings.json", "{" if registration == "broken" else json.dumps(data))
    expected = None if registration == "missing-manifest" else "0.7.0" if registration in ("directory", "disabled-value") else "9.0.0"
    assert _targets.installed_version(tmp_path, tool) == expected


@pytest.fixture
def channel_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PersonalisationSettings, Path, list[ToolDescriptor]]:
    home = tmp_path / "home"
    owner = settings(home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setattr(_status, "_session", lambda: ("claude", "claude --resume"))
    return owner, home, _status.load_descriptors(ToolDescriptor, "tool.json")


# Channel binding warnings outside a managed repository.
def test_bound_channel_without_launch_flag_warns_once(channel_session: tuple[PersonalisationSettings, Path, list[ToolDescriptor]], tmp_path: Path) -> None:
    owner, home, tools = channel_session
    assert _status.channel_warnings(owner, home, tools) == [WARNING]
    assert _status.status_lines(status(tmp_path, home))[-2:] == ["repo none", WARNING]


# Moved assertions 7-8: home expansion, trailing separators, and the platform comparer.
@pytest.mark.parametrize("variant", ["trailing", "home", "separators", "case"])
def test_channel_account_path_matching(channel_session: tuple[PersonalisationSettings, Path, list[ToolDescriptor]], monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    owner, home, tools = channel_session
    path = str(home / ".claude")
    if variant == "trailing":
        path += os.sep
    elif variant == "home":
        path = "~/.claude"
    elif variant == "separators":
        path = path.replace("\\", "/")
    else:
        path = path.upper()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", path)
    assert _status.channel_warnings(owner, home, tools) == ([] if variant == "case" and sys.platform != "win32" else [WARNING])


# Moved assertions 9-16: connected, unknown, unreadable and ambiguous sessions are silent.
@pytest.mark.parametrize("reason", ["connected", "runtime", "case-sensitive-tool", "other-account", "member", "unmatched", "unsupported", "empty", "ambiguous", "no-config", "no-launch", "bad-launch"])
def test_uncertain_or_connected_sessions_are_silent(channel_session: tuple[PersonalisationSettings, Path, list[ToolDescriptor]], monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    owner, home, tools = channel_session
    if reason == "connected":
        monkeypatch.setattr(_status, "_session", lambda: ("claude", "claude --channels plugin:telegram"))
    elif reason in ("runtime", "case-sensitive-tool"):
        monkeypatch.setattr(_status, "_session", lambda: ("node" if reason == "runtime" else "Claude", "--resume"))
    elif reason == "other-account":
        owner.channels[0].accounts = ["codex-a"]
    elif reason == "member":
        monkeypatch.setenv("LEGION_ROLE", "member")
        monkeypatch.setenv("LEGION_TASK", "task.md")
    elif reason == "unmatched":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "unknown"))
    elif reason in ("unsupported", "empty"):
        monkeypatch.setattr(_status, "_session", lambda: None if reason == "unsupported" else ("claude", ""))
    elif reason == "ambiguous":
        owner.agents.append(owner.agents[0].model_copy(update={"id": "claude-b"}))
    elif reason == "no-config":
        for tool in tools:
            monkeypatch.delenv(tool.config_env, raising=False)
    else:
        descriptor = ChannelDescriptor.model_validate({"schemaVersion": 1, "id": "telegram", "launch": {"claude": [] if reason == "no-launch" else ["some prose"]}})
        monkeypatch.setattr(_status, "load_descriptors", lambda *_args: [descriptor])
    assert _status.channel_warnings(owner, home, tools) == []


# Settings and tool descriptor validation.
@pytest.mark.parametrize("broken", ["settings", "process", "tool", "channel"])
def test_broken_inputs_preserve_the_rest_of_status(channel_session: tuple[PersonalisationSettings, Path, list[ToolDescriptor]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str) -> None:
    _owner, home, _tools = channel_session

    def fail() -> Optional[tuple[str, str]]:
        raise OSError("unreadable session")

    if broken == "settings":
        write(home / ".legion/configuration/personalisation.json", "{")
    elif broken == "process":
        monkeypatch.setattr(_status, "_session", fail)
    else:
        root = tmp_path / "malformed plugin"
        source = REPO / "plugins/legion/skills"
        for path in source.glob("legion-*/*.json"):
            write(root / "skills" / path.parent.name / path.name, path.read_text(encoding="utf-8"))
        file = "legion-claude/tool.json" if broken == "tool" else "legion-telegram/channel.json"
        write(root / "skills" / file, "{")
        monkeypatch.setattr(_validate, "PLUGIN_ROOT", root)
    lines = _status.status_lines(status(tmp_path, home))
    if broken == "settings":
        assert lines[0] == f"legion settings are not valid: {home / '.legion/configuration/personalisation.json'}; run legion setup"
    assert lines[-1] == "repo none"
    assert any(line.startswith("knowledge:") for line in lines)
    assert WARNING not in lines


# Moved assertions 1-4: use the lease owner, preserve native identity and read only its command line.
@pytest.mark.skipif(sys.platform != "win32", reason="Windows native session reader")
def test_session_queries_only_the_lease_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _process.ProcessInfo(123, 1, "claude.exe", datetime.now(UTC))
    monkeypatch.setattr(_process, "owner_process", lambda: owner)
    queried = []

    def command(pid: int) -> str:
        queried.append(pid)
        return "claude --resume"

    monkeypatch.setattr(_status, "_read_command_line", command)
    assert _status._session() == ("claude", "claude --resume")
    assert queried == [123]


def test_native_command_line_and_unreadable_pid() -> None:
    assert "python" in _status._read_command_line(os.getpid()).lower()
    assert _status._read_command_line(99_999_999) == ""


# Moved assertion 5: unsupported platforms never inspect the process tree or its arguments.
@pytest.mark.skipif(sys.platform == "win32", reason="Unsupported session platform")
def test_non_windows_does_not_inspect_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> _process.ProcessInfo:
        pytest.fail("unsupported platform inspected the session")

    monkeypatch.setattr(_process, "owner_process", fail)
    assert _status._session() is None


def test_status_schema_omits_legacy_launcher_fields() -> None:
    assert "on_path" not in status_service.StatusReport.model_fields
    assert "commands" not in status_service.StatusReport.model_fields
