import json
import os
import subprocess
import stat
import sys
import tempfile
import time
from threading import Barrier
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import (
    Any,
    Optional,
)

import pytest
from cli_runner import run
from conftest import REPO, Fence

from legion.core._home import home_path
from legion.core._process import process_started_at
from legion.core._run import RunResult
from legion.core._settings_models import Agent
from legion.services import limits as limits_service
from legion.services.limits.limit_probes import (
    LimitsError,
    LimitRow,
    _ping,
    account_rows,
    claude,
    codex,
)
from legion.services.limits import limits
from legion.services.limits.limit_probes._ping import Ping, format_used_percent

FIXTURES = REPO / "tests" / "fixtures" / "limits"
FAKE_CLI = Path(__file__).with_name("limits_fake_cli.py")
ACCESS_VALUE = "fake-access-value"
TOKEN_ARGUMENTS = ["account", "get-access-token", "--resource", "https://cognitiveservices.azure.com", "-o", "json"]
CONFIG_ENVIRONMENT = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "copilot": "COPILOT_HOME"}
FOUNDRY = {"kind": "foundry", "endpoint": "https://my-resource.cognitiveservices.azure.com", "default": "model-a", "deployments": [{"name": "model-a"}]}


def reset_time(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).astimezone().strftime("%Y-%m-%d %H:%M")


# A fake executable that names itself to the fake CLI: <name>.cmd on Windows, a shell script elsewhere.
def write_executable(folder: Path, name: str, environment: dict[str, str]) -> Path:
    environment = {"LEGION_FAKE_NAME": name.split("-", maxsplit=1)[0], **environment}
    if sys.platform == "win32":
        path = folder / f"{name}.cmd"
        lines = ["@echo off", *(f'set "{key}={value}"' for key, value in environment.items()), f'"{sys.executable}" "{FAKE_CLI}" %*', "exit /b %ERRORLEVEL%"]
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        path = folder / name
        lines = ["#!/bin/sh", *(f"export {key}='{value}'" for key, value in environment.items()), f"exec '{sys.executable}' '{FAKE_CLI}' \"$@\""]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Bench:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.home = root / "home"
        self.path = root / "bin"
        self.logs = root / "logs"
        self.azure = root / "azure"
        for folder in (self.home, self.path, self.logs, self.azure):
            folder.mkdir(parents=True)
        for name in [name for name in os.environ if name.startswith("LEGION_FAKE_")]:
            monkeypatch.delenv(name)
        # Only the fakes can be found: the real PATH is gone.
        monkeypatch.setenv("PATH", str(self.path))
        monkeypatch.setenv("LEGION_FAKE_LOG", str(self.logs))
        monkeypatch.setenv("LEGION_FAKE_HOME", str(self.home))
        monkeypatch.setenv("AZURE_CONFIG_DIR", str(self.azure))
        # The stock CLIs; legion's account launch sets each account's config folder itself.
        for name in ("git", "az", *CONFIG_ENVIRONMENT):
            write_executable(self.path, name, {})

    def config(self, account: str) -> Path:
        return self.home / ".legion" / "agents" / account

    def fake(self, **settings: str) -> None:
        for name, value in settings.items():
            self.monkeypatch.setenv(f"LEGION_FAKE_{name}", value)

    def calls(self, name: str) -> list[dict[str, Any]]:
        log = self.logs / f"{name}.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def rows(self, account: str, tool: str, timeout: int = 60, **fields: Any) -> list[LimitRow]:
        agent = Agent.model_validate({"id": account, "tool": tool, "configDir": f"~/.legion/agents/{account}", **fields})
        return account_rows(agent, self.home, timeout)


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    return Bench(tmp_path, monkeypatch)


@pytest.mark.parametrize("selection", [[], [""], [" "], ["nobody"], ["claude-a", "nobody"], ["claude-a", ""]])
def test_limits_prevalidates_whole_selection_without_probes(bench: Bench, selection: list[str]) -> None:
    write_settings(bench.home, ACCOUNTS)
    expected = "name at least one account" if not selection else f"unknown account '{selection[-1]}'"
    with pytest.raises(LimitsError, match=expected):
        limits(bench.home, selection)
    assert [bench.calls(name) for name in ("claude", "codex", "copilot", "az", "git")] == [[], [], [], [], []]


@pytest.mark.parametrize("selection", ["ab", "a", b"ab"])
def test_limits_rejects_scalar_selection_without_probes(bench: Bench, selection: Any) -> None:
    write_settings(bench.home, [{"id": account, "tool": "claude", "configDir": f"~/{account}"} for account in ("a", "b")])

    with pytest.raises(LimitsError, match="pass account ids as a list or tuple"):
        limits(bench.home, selection)
    assert not list(bench.logs.iterdir())


@pytest.mark.parametrize("interface", ["service", "text", "json", "manual"])
def test_limits_rejects_duplicate_configured_ids_without_probes(bench: Bench, interface: str, capsys: pytest.CaptureFixture[str]) -> None:
    # The second definition would change a subscription check into a provider check.
    agents = [ACCOUNTS[2], ACCOUNTS[0], {**ACCOUNTS[3], "id": "claude-a"}]
    write_settings(bench.home, agents)
    if interface == "service":
        with pytest.raises(LimitsError, match="duplicate account id 'claude-a'"):
            limits(bench.home, ["copilot-a", "claude-a"])
    else:
        words = [] if interface == "manual" else ["--id", "copilot-a", "--id", "claude-a"]
        if interface == "json":
            words.append("--json")
        assert run(["--home", str(bench.home), "limits", *words]) == 1
        assert "duplicate account id 'claude-a'" in capsys.readouterr().err
    assert not list(bench.logs.iterdir())


def test_limits_returns_rows_per_account_in_requested_order(bench: Bench) -> None:
    write_settings(bench.home, ACCOUNTS)
    reports = limits(bench.home, ["copilot-a", "claude-a"], timeout=10)
    assert [(item.account, item.tool) for item in reports] == [("copilot-a", "copilot"), ("claude-a", "claude")]
    assert reports[0].rows == [LimitRow(status="answers; no limit readout")]
    assert [len(bench.calls(name)) for name in ("claude", "codex", "copilot", "az")] == [1, 0, 1, 0]


def test_limits_probes_concurrently_in_requested_order(bench: Bench) -> None:
    write_settings(bench.home, ACCOUNTS)
    started = Barrier(2, timeout=2)

    def probe(agent: Agent, home: Path, timeout: float) -> list[LimitRow]:
        assert home == bench.home
        assert timeout == 7
        started.wait()
        return [LimitRow(status=agent.id)]

    bench.monkeypatch.setattr(limits_service, "account_rows", probe)
    reports = limits(bench.home, ["copilot-a", "claude-a"], timeout=7)
    assert [(report.account, report.rows[0].status) for report in reports] == [("copilot-a", "copilot-a"), ("claude-a", "claude-a")]


@pytest.mark.parametrize(
    "words",
    [
        ["--json"],
        ["--id", ""],
        ["--id", " "],
        ["--json", "--id", ""],
        ["--id", "claude-a", "--id", ""],
        ["--id", "claude-a", "--id", "nobody"],
        ["--json", "--id", "claude-a", "--id", "nobody"],
    ],
)
def test_automated_cli_rejects_missing_or_invalid_selection_before_probes(bench: Bench, words: list[str]) -> None:
    write_settings(bench.home, ACCOUNTS)
    assert run(["--home", str(bench.home), "limits", *words]) == 1
    assert not list(bench.logs.iterdir())


@pytest.mark.parametrize(("environment", "value"), [("LEGION_ROLE", "member"), ("LEGION_TASK", "task.md")])
def test_bare_text_cli_lists_every_account_in_any_role(bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], environment: str, value: str) -> None:
    write_settings(bench.home, ACCOUNTS)
    monkeypatch.setenv(environment, value)
    assert run(["--home", str(bench.home), "limits"]) == 0
    output = capsys.readouterr().out
    assert "claude-a" in output
    assert "copilot-a" in output


def test_cli_limits_json_repeatable_id(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    write_settings(bench.home, ACCOUNTS)
    expected = limits(bench.home, ["copilot-a", "claude-a"])
    assert run(["--home", str(bench.home), "limits", "--id", "copilot-a", "--id", "claude-a", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [item.model_dump(mode="json") for item in expected]
    assert [len(bench.calls(name)) for name in ("claude", "codex", "copilot", "az")] == [2, 0, 2, 0]


def test_cli_limits_help_explains_probe_cost_and_side_effects(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["limits", "--help"]) == 0
    output = " ".join(capsys.readouterr().out.split())
    for text in ("quota", "billable", "disposable", "session record", "az account get-access-token", "120", "--id", "--json", "bare text call selects all accounts", "MCP tool require explicit"):
        assert text in output


def assert_disposable_folder(folder: str) -> None:
    assert Path(folder).name.startswith("legion-limits-")
    assert Path(folder).parent.resolve() == Path(tempfile.gettempdir()).resolve()
    assert not Path(folder).exists()


def test_claude_reads_every_unified_window(bench: Bench) -> None:
    bench.fake(STDOUT=str(FIXTURES / "claude-stream.jsonl"))
    assert bench.rows("claude-a", "claude") == [
        LimitRow(window="five_hour", used="69%", resets=reset_time(1795350000), status="allowed"),
        LimitRow(window="seven_day", used="55%", resets=reset_time(1795654800), status="allowed"),
    ]
    [call] = bench.calls("claude")
    assert call["arguments"] == ["-p", "--output-format", "stream-json", "--verbose", "ping"]
    assert call["stdin"] == ""
    assert Path(call["config"]["CLAUDE_CONFIG_DIR"]).resolve() == bench.config("claude-a").resolve()
    assert_disposable_folder(call["folder"])
    assert bench.calls("git") == []


def test_fake_cli_does_not_log_inherited_secret_or_read_external_provider(bench: Bench, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    external = tmp_path / "external-provider.json"
    external.write_text("private-provider-content", encoding="utf-8")
    monkeypatch.setenv("COPILOT_HOME", str(bench.config("copilot-a")))
    monkeypatch.setenv("COPILOT_PROVIDERS_CONFIG", str(external))
    monkeypatch.setenv("UNRELATED_SECRET", "private-environment-content")
    monkeypatch.setenv("LEGION_FAKE_NAME", "copilot")
    subprocess.run([sys.executable, str(FAKE_CLI)], input="", text=True, check=True)
    [call] = bench.calls("copilot")
    assert "private-environment-content" not in json.dumps(call)
    assert call["providers"] is None


def test_claude_without_an_event_has_no_readout(bench: Bench) -> None:
    bench.fake(STDOUT=str(FIXTURES / "claude-no-event.jsonl"))
    assert bench.rows("claude-a", "claude") == [LimitRow(status="no limit readout")]


def test_claude_event_without_windows_names_its_type(bench: Bench, tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1795350000, "rateLimitType": "five_hour"}}
    stream.write_text("not json\n" + json.dumps(event) + "\n", encoding="utf-8")
    bench.fake(STDOUT=str(stream), EXIT="1")
    assert bench.rows("claude-a", "claude") == [LimitRow(window="five_hour", used="unknown", resets=reset_time(1795350000), status="rejected")]


def test_claude_failures_tell_the_quota_from_other_errors(bench: Bench, tmp_path: Path) -> None:
    quota = tmp_path / "quota.txt"
    quota.write_text("Claude AI usage limit reached|1795350000\n", encoding="utf-8")
    bench.fake(STDOUT=str(quota), EXIT="1")
    assert bench.rows("claude-a", "claude") == [LimitRow(status="limit reached")]
    failure = tmp_path / "failure.txt"
    failure.write_text("\n  Invalid API key  \nsecond line\n", encoding="utf-8")
    bench.fake(STDOUT="", STDERR=str(failure))
    assert bench.rows("claude-a", "claude") == [LimitRow(status="ping failed: Invalid API key")]


def test_codex_reads_only_its_own_session(bench: Bench) -> None:
    bench.fake(SESSION=str(FIXTURES / "codex-session.jsonl"), CONCURRENT="1")
    assert bench.rows("codex-a", "codex") == [
        LimitRow(window="5h", used="12%", resets=reset_time(1795350000), status="unknown"),
        LimitRow(window="7d", used="40%", resets=reset_time(1795654800), status="unknown"),
    ]
    [call] = bench.calls("codex")
    assert call["arguments"] == ["exec", "ping"]
    assert call["stdin"] == ""
    assert Path(call["config"]["CODEX_HOME"]).resolve() == bench.config("codex-a").resolve()
    assert_disposable_folder(call["folder"])
    # Codex needs a git folder: the ping folder is initialised first.
    [git] = bench.calls("git")
    assert git["arguments"][:1] == ["-C"]
    assert Path(git["arguments"][1]).resolve() == Path(call["folder"]).resolve()
    assert git["arguments"][2:] == ["init", "-q"]


def test_codex_null_windows_have_no_readout(bench: Bench) -> None:
    bench.fake(SESSION=str(FIXTURES / "codex-session-null.jsonl"))
    assert bench.rows("codex-a", "codex") == [LimitRow(status="no limit readout")]


def test_codex_quota_error_names_the_retry_time(bench: Bench) -> None:
    bench.fake(STDERR=str(FIXTURES / "codex-quota.txt"), EXIT="1")
    assert bench.rows("codex-a", "codex") == [LimitRow(resets="2:57 PM", status="limit reached")]


@pytest.mark.parametrize(
    ("fake", "timeout", "status"),
    [({"GIT_EXIT": "5"}, 60, "error: git init failed (exit code 5)"), ({"GIT_SLEEP": "60"}, 10, "error: git init timed out")],
    ids=["exit-code", "timeout"],
)
def test_codex_ping_stops_when_git_init_fails(bench: Bench, fake: dict[str, str], timeout: int, status: str) -> None:
    bench.fake(**fake)
    assert bench.rows("codex-a", "codex", timeout=timeout) == [LimitRow(status=status)]
    assert len(bench.calls("git")) == 1
    assert bench.calls("codex") == []


def test_codex_init_and_ping_share_one_deadline(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []
    real_run = _ping.run_command

    def spy(command: str, arguments: Sequence[str], timeout: Optional[float] = None, env: Optional[Mapping[str, str]] = None, cwd: Optional[Path] = None) -> RunResult:
        assert timeout is not None
        timeouts.append(timeout)
        time.sleep(1)
        return real_run(command, arguments, timeout=timeout, env=env, cwd=cwd)

    monkeypatch.setattr(_ping, "run_command", spy)
    bench.fake(SESSION=str(FIXTURES / "codex-session.jsonl"))
    bench.rows("codex-a", "codex", timeout=30)
    assert len(timeouts) == 2
    assert timeouts[0] <= 30
    assert timeouts[1] <= 29


def test_codex_without_git_is_an_error(bench: Bench) -> None:
    next(bench.path.glob("git*")).unlink()
    assert bench.rows("codex-a", "codex") == [LimitRow(status="error: git is not installed")]
    assert bench.calls("codex") == []


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected"),
    [(1, False, "ping failed: probe failed"), (-1, True, "ping timed out"), (0, False, "no limit readout")],
    ids=["failed", "timeout", "unrelated session"],
)
def test_codex_never_reuses_another_sessions_windows(tmp_path: Path, exit_code: int, timed_out: bool, expected: str) -> None:
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "old.jsonl").write_text((FIXTURES / "codex-session.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    probe = tmp_path / "probe"
    probe.mkdir()
    ping = Ping(exit_code=exit_code, output="", error="probe failed", timed_out=timed_out, folder=probe.resolve())
    assert codex.rows(ping, tmp_path / "codex") == [LimitRow(status=expected)]


def test_codex_matches_a_session_named_through_a_link(fence: Fence) -> None:
    # The session names the probe through the fence's linked home; the ping folder is the physical path.
    probe = fence.temp / "legion-limits-linked"
    probe.mkdir()
    sessions = fence.temp / "codex" / "sessions"
    sessions.mkdir(parents=True)
    records = (FIXTURES / "codex-session.jsonl").read_text(encoding="utf-8").splitlines()[1:]
    metadata = json.dumps({"type": "session_meta", "payload": {"cwd": str(probe)}})
    (sessions / "probe.jsonl").write_text("\n".join([metadata, *records]) + "\n", encoding="utf-8")
    assert probe.resolve() != probe
    rows = codex.rows(Ping(exit_code=0, output="", error="", timed_out=False, folder=probe.resolve()), fence.temp / "codex")
    assert [(row.window, row.used) for row in rows] == [("5h", "12%"), ("7d", "40%")]


@pytest.mark.parametrize(("minutes", "expected"), [(300, "5h"), (10080, "7d"), (90, "90m"), (None, "primary")])
def test_codex_window_name_from_its_length(minutes: Optional[int], expected: str) -> None:
    assert codex.window_name(minutes, "primary") == expected


def test_copilot_answer_has_no_readout(bench: Bench) -> None:
    assert bench.rows("copilot-a", "copilot") == [LimitRow(status="answers; no limit readout")]
    [call] = bench.calls("copilot")
    assert call["arguments"] == ["-p", "ping", "-s"]
    assert call["stdin"] == ""
    assert Path(call["config"]["COPILOT_HOME"]).resolve() == bench.config("copilot-a").resolve()
    assert_disposable_folder(call["folder"])


def test_copilot_quota_is_the_limit_and_other_failures_are_not(bench: Bench) -> None:
    bench.fake(STDERR=str(FIXTURES / "copilot-quota.txt"), EXIT="1")
    assert bench.rows("copilot-a", "copilot") == [LimitRow(status="limit reached")]
    bench.fake(STDERR=str(FIXTURES / "codex-session-null.jsonl"))
    [row] = bench.rows("copilot-a", "copilot")
    assert row.status.startswith("ping failed: ")


def test_slow_ping_times_out_and_ends_its_whole_tree(bench: Bench) -> None:
    bench.fake(SLEEP="60")
    started = time.monotonic()
    assert bench.rows("copilot-a", "copilot", timeout=3) == [LimitRow(status="ping timed out")]
    assert time.monotonic() - started < 30
    grandchild = int((bench.logs / "grandchild.pid").read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while process_started_at(grandchild) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert process_started_at(grandchild) is None
    assert_disposable_folder(bench.calls("copilot")[0]["folder"])


def test_foundry_token_ok_never_shows_the_token(bench: Bench, tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    answer = tmp_path / "token.json"
    answer.write_text(json.dumps({"accessToken": ACCESS_VALUE, "expiresOn": "2026-11-18T18:30:00+00:00"}), encoding="utf-8")
    bench.fake(AZ_STDOUT=str(answer))
    rows = bench.rows("foundry-a", "claude", provider=FOUNDRY)
    assert rows == [LimitRow(used="-", resets="-", status="token ok")]
    [call] = bench.calls("az")
    assert call["arguments"] == TOKEN_ARGUMENTS
    assert Path(call["config"]["AZURE_CONFIG_DIR"]).resolve() == bench.azure.resolve()
    captured = capfd.readouterr()
    assert ACCESS_VALUE not in captured.out + captured.err + repr(rows)
    assert bench.calls("claude") == []


def test_foundry_failure_gives_a_fixed_error_without_login(bench: Bench, tmp_path: Path) -> None:
    error = tmp_path / "error.txt"
    error.write_text("ERROR: Please run 'az login' to setup account.\nmore detail\n", encoding="utf-8")
    bench.fake(AZ_STDERR=str(error), AZ_EXIT="1")
    assert bench.rows("foundry-a", "codex", provider=FOUNDRY) == [LimitRow(used="-", resets="-", status="az could not get a token (exit code 1)")]
    assert [call["arguments"] for call in bench.calls("az")] == [TOKEN_ARGUMENTS]


@pytest.mark.parametrize("stream", ["AZ_STDOUT", "AZ_STDERR"])
def test_foundry_failure_never_shows_az_output(bench: Bench, tmp_path: Path, capfd: pytest.CaptureFixture[str], stream: str) -> None:
    answer = tmp_path / "answer.txt"
    answer.write_text(json.dumps({"accessToken": ACCESS_VALUE}), encoding="utf-8")
    bench.fake(**{stream: str(answer)}, AZ_EXIT="3")
    rows = bench.rows("foundry-a", "claude", provider=FOUNDRY)
    assert rows == [LimitRow(used="-", resets="-", status="az could not get a token (exit code 3)")]
    captured = capfd.readouterr()
    assert ACCESS_VALUE not in captured.out + captured.err + repr(rows)


def test_foundry_answer_without_a_token_is_not_ok(bench: Bench, tmp_path: Path) -> None:
    answer = tmp_path / "empty.json"
    answer.write_text("{}", encoding="utf-8")
    bench.fake(AZ_STDOUT=str(answer))
    assert bench.rows("foundry-a", "claude", provider=FOUNDRY)[0].status == "az returned no access token"
    answer.write_text("not json", encoding="utf-8")
    assert bench.rows("foundry-a", "claude", provider=FOUNDRY)[0].status == "az returned no access token"


def test_foundry_without_az_names_the_install(bench: Bench) -> None:
    next(bench.path.glob("az*")).unlink()
    assert bench.rows("foundry-a", "copilot", provider=FOUNDRY)[0].status == "az is not installed; ask legion to install the Azure CLI"


# The ping runs the account's own program when it has one, with its config folder; the stock CLI is not used then.
def test_limits_rows_use_account_command(bench: Bench) -> None:
    own = bench.path.parent / "own"
    own.mkdir()
    command = write_executable(own, "claude", {"LEGION_FAKE_LOG": str(own)})
    write_executable(bench.path, "claude", {"LEGION_FAKE_CLAUDE_EXIT": "99"})
    bench.fake(STDOUT=str(FIXTURES / "claude-stream.jsonl"))
    assert bench.rows("claude-a", "claude", command=str(command))[0].window == "five_hour"
    assert bench.calls("claude") == []
    [call] = [json.loads(line) for line in (own / "claude.jsonl").read_text(encoding="utf-8").splitlines()]
    assert Path(call["config"]["CLAUDE_CONFIG_DIR"]).resolve() == bench.config("claude-a").resolve()


def test_missing_program_names_the_installer(bench: Bench) -> None:
    next(bench.path.glob("claude*")).unlink()
    assert bench.rows("claude-a", "claude") == [LimitRow(status="error: claude is not installed; run legion tool install --tool claude")]


def test_tool_without_a_recipe_says_so(bench: Bench) -> None:
    assert bench.rows("claude-a", "gemini") == [LimitRow(status="no limits reader for gemini")]


@pytest.mark.parametrize(("percent", "expected"), [(12.0, "12%"), (12.5, "12%"), (13.5, "14%"), (68.99999, "69%")])
def test_used_percent_rounds_like_powershell(percent: float, expected: str) -> None:
    assert format_used_percent(percent) == expected


@pytest.mark.parametrize(
    ("used", "left"),
    [("38%", "62%"), ("0%", "100%"), ("100%", "0%"), ("120%", "0%"), ("-5%", "100%"), ("unknown", "unknown"), ("-", "-")],
)
def test_left_is_what_the_used_percent_leaves(used: str, left: str) -> None:
    assert LimitRow(used=used).left == left


def test_json_row_has_used_and_left() -> None:
    assert LimitRow(window="5h", used="38%").model_dump(mode="json") == {"window": "5h", "used": "38%", "left": "62%", "resets": "unknown", "status": "unknown"}


def test_codex_skips_unreadable_session_lines(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.mkdir()
    sessions = tmp_path / "codex" / "sessions"
    (sessions / "0-folder.jsonl").mkdir(parents=True)
    (sessions / "a-broken.jsonl").write_text("not json\n", encoding="utf-8")
    (sessions / "b-list.jsonl").write_text("[1]\n", encoding="utf-8")
    metadata = json.dumps({"type": "session_meta", "payload": {"cwd": str(probe)}})
    records = (FIXTURES / "codex-session.jsonl").read_text(encoding="utf-8").splitlines()[1:]
    (sessions / "c-probe.jsonl").write_text("\n".join([metadata, *records, '{"rate_limits": broken', '["rate_limits"]']) + "\n", encoding="utf-8")
    rows = codex.rows(Ping(exit_code=0, output="", error="", timed_out=False, folder=probe.resolve()), tmp_path / "codex")
    assert [(row.window, row.used) for row in rows] == [("5h", "12%"), ("7d", "40%")]


def test_codex_reads_past_session_files_it_cannot_read_or_decode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = tmp_path / "probe"
    probe.mkdir()
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "a-binary.jsonl").write_bytes(b"\xff\xfe not utf-8\n")
    locked = sessions / "b-locked.jsonl"
    locked.write_text("{}\n", encoding="utf-8")
    metadata = json.dumps({"type": "session_meta", "payload": {"cwd": str(probe)}})
    records = (FIXTURES / "codex-session.jsonl").read_text(encoding="utf-8").splitlines()[1:]
    (sessions / "c-probe.jsonl").write_text("\n".join([metadata, *records]) + "\n", encoding="utf-8")
    real_open = Path.open

    def refuse_locked(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == locked:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_locked)
    rows = codex.rows(Ping(exit_code=0, output="", error="", timed_out=False, folder=probe.resolve()), tmp_path / "codex")
    assert [(row.window, row.used) for row in rows] == [("5h", "12%"), ("7d", "40%")]


def test_codex_reads_past_session_files_whose_metadata_is_refused(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.mkdir()
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    refused = sessions / "a-refused.jsonl"
    refused.write_text("{}\n", encoding="utf-8")
    metadata = json.dumps({"type": "session_meta", "payload": {"cwd": str(probe)}})
    records = (FIXTURES / "codex-session.jsonl").read_text(encoding="utf-8").splitlines()[1:]
    (sessions / "b-probe.jsonl").write_text("\n".join([metadata, *records]) + "\n", encoding="utf-8")
    real_stat = Path.stat

    def refuse_metadata(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == refused:
            raise PermissionError(13, "Permission denied")
        return real_stat(path, follow_symlinks=follow_symlinks)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "stat", refuse_metadata)
        rows = codex.rows(Ping(exit_code=0, output="", error="", timed_out=False, folder=probe.resolve()), tmp_path / "codex")
    assert [(row.window, row.used) for row in rows] == [("5h", "12%"), ("7d", "40%")]


# Only the latest valid record counts; a later record of another shape does not hide it.
def test_codex_keeps_the_latest_valid_record(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.mkdir()
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    metadata = json.dumps({"type": "session_meta", "payload": {"cwd": str(probe)}})
    records = (FIXTURES / "codex-session.jsonl").read_text(encoding="utf-8").splitlines()[1:]
    later = json.dumps({"rate_limits": {"primary": {"used_percent": 77.0, "window_minutes": 300}}})
    (sessions / "probe.jsonl").write_text("\n".join([metadata, *records, later, '{"rate_limits": broken']) + "\n", encoding="utf-8")
    rows = codex.rows(Ping(exit_code=0, output="", error="", timed_out=False, folder=probe.resolve()), tmp_path / "codex")
    assert [(row.window, row.used) for row in rows] == [("5h", "77%")]


def test_foundry_az_that_never_answers_is_stopped(bench: Bench) -> None:
    bench.fake(AZ_SLEEP="60")
    assert bench.rows("foundry-a", "claude", timeout=3, provider=FOUNDRY) == [LimitRow(used="-", resets="-", status="az timed out")]


def test_account_folder_starts_at_the_home_root(tmp_path: Path) -> None:
    assert home_path("~/.legion/agents/codex-a", tmp_path).resolve() == (tmp_path / ".legion/agents/codex-a").resolve()
    assert home_path("~\\agents", tmp_path).resolve() == (tmp_path / "agents").resolve()
    assert home_path(str(tmp_path / "elsewhere"), tmp_path).resolve() == (tmp_path / "elsewhere").resolve()


def write_settings(home: Path, agents: list[dict[str, Any]]) -> None:
    file = home / ".legion" / "configuration" / "personalisation.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"schemaVersion": 1, "agents": agents, "channels": []}), encoding="utf-8")


ACCOUNTS = [
    {"id": "claude-a", "tool": "claude", "configDir": "~/.legion/agents/claude-a"},
    {"id": "codex-a", "tool": "codex", "configDir": "~/.legion/agents/codex-a"},
    {"id": "copilot-a", "tool": "copilot", "configDir": "~/.legion/agents/copilot-a"},
    {"id": "foundry-a", "tool": "claude", "configDir": "~/.legion/agents/foundry-a", "provider": FOUNDRY},
]

# The table the PowerShell limits command printed for these fakes (Format-Table -AutoSize): a blank line first,
# one space between columns, the last column not padded.
EXPECTED_TABLE = """
Account   Tool    Window    Left    Resets at        Status
-------   ----    ------    ----    ---------        ------
claude-a  claude  five_hour 31%     {five_hour} allowed
claude-a  claude  seven_day 45%     {seven_day} allowed
codex-a   codex   5h        88%     {five_hour} unknown
codex-a   codex   7d        60%     {seven_day} unknown
copilot-a copilot -         unknown unknown          answers; no limit readout
foundry-a claude  -         -       -                token ok
"""


def test_limits_prints_the_powershell_table_for_every_account(bench: Bench, tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    write_settings(bench.home, ACCOUNTS)
    answer = tmp_path / "token.json"
    answer.write_text(json.dumps({"accessToken": ACCESS_VALUE, "expiresOn": "2026-11-18T18:30:00+00:00"}), encoding="utf-8")
    bench.fake(CLAUDE_STDOUT=str(FIXTURES / "claude-stream.jsonl"), CODEX_SESSION=str(FIXTURES / "codex-session.jsonl"), AZ_STDOUT=str(answer))
    assert run(["--home", str(bench.home), "limits"]) == 0
    captured = capfd.readouterr()
    assert captured.out == EXPECTED_TABLE.format(five_hour=reset_time(1795350000), seven_day=reset_time(1795654800))
    assert captured.err == ""
    assert ACCESS_VALUE not in captured.out + captured.err
    assert [len(bench.calls(name)) for name in ("claude", "codex", "copilot", "az")] == [1, 1, 1, 1]


def test_limits_for_one_account_pings_only_that_account(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    write_settings(bench.home, ACCOUNTS)
    assert run(["--home", str(bench.home), "limits", "--id", "copilot-a"]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == [
        "Account   Tool    Window Left    Resets at Status",
        "-------   ----    ------ ----    --------- ------",
        "copilot-a copilot -      unknown unknown   answers; no limit readout",
    ]
    assert [len(bench.calls(name)) for name in ("claude", "codex", "copilot", "az")] == [0, 0, 1, 0]


@pytest.mark.parametrize(
    ("agents", "words", "message"),
    [
        (None, [], "No legion settings yet; run legion setup."),
        (ACCOUNTS, ["--id", "nobody"], "unknown account 'nobody'"),
        (ACCOUNTS, ["--id", "Claude-A"], "unknown account 'Claude-A'"),
    ],
    ids=["no settings", "unknown account", "account names are exact"],
)
def test_limits_errors_exit_one(bench: Bench, capsys: pytest.CaptureFixture[str], agents: list[dict[str, Any]] | None, words: list[str], message: str) -> None:
    if agents is not None:
        write_settings(bench.home, agents)
    assert run(["--home", str(bench.home), "limits", *words]) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", message + "\n")
    assert bench.calls("claude") == []


def test_limits_with_unreadable_settings_exits_one(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    write_settings(bench.home, [{"id": "claude-a"}])
    assert run(["--home", str(bench.home), "limits"]) == 1
    assert capsys.readouterr().err.startswith("legion settings are not valid: ")


def test_limits_without_accounts_says_to_add_one(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    write_settings(bench.home, [])
    assert run(["--home", str(bench.home), "limits"]) == 0
    assert capsys.readouterr().out == "No accounts yet; run legion add.\n"


@pytest.mark.parametrize("words", [["claude-a"], ["--id", "claude-a", "codex-a"], ["--yes"]], ids=["positional id", "extra positional", "a flag"])
def test_limits_takes_at_most_one_named_account_and_no_other_flags(bench: Bench, words: list[str]) -> None:
    write_settings(bench.home, ACCOUNTS)
    assert run(["--home", str(bench.home), "limits", *words]) == 2
    assert bench.calls("claude") == []


# A line whose shape does not match is skipped like unreadable JSON; the last readable event still counts.
def test_claude_skips_an_event_of_the_wrong_shape(tmp_path: Path) -> None:
    good = (FIXTURES / "claude-stream.jsonl").read_text(encoding="utf-8")
    wrong = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": "high", "resetsAt": "tomorrow"}}}})
    rows = claude.rows(Ping(exit_code=0, output=good + wrong + "\n", error="", timed_out=False, folder=tmp_path))
    assert [(row.window, row.used) for row in rows] == [("five_hour", "69%"), ("seven_day", "55%")]
    assert claude.rows(Ping(exit_code=0, output=wrong + "\n", error="", timed_out=False, folder=tmp_path)) == [LimitRow(status="no limit readout")]


def test_codex_skips_limits_of_the_wrong_shape(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.mkdir()
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    metadata = json.dumps({"type": "session_meta", "payload": {"cwd": str(probe)}})
    wrong = json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": "lots", "resets_at": "soon"}}}})
    (sessions / "probe.jsonl").write_text(metadata + "\n" + wrong + "\n", encoding="utf-8")
    assert codex.rows(Ping(exit_code=0, output="", error="", timed_out=False, folder=probe.resolve()), tmp_path / "codex") == [LimitRow(status="no limit readout")]


@pytest.mark.parametrize("answer", ['{"accessToken": 42}', '{"accessToken": ""}', "[]"])
def test_foundry_token_of_the_wrong_shape_is_not_ok(bench: Bench, tmp_path: Path, answer: str) -> None:
    file = tmp_path / "answer.json"
    file.write_text(answer, encoding="utf-8")
    bench.fake(AZ_STDOUT=str(file))
    assert bench.rows("foundry-a", "claude", provider=FOUNDRY)[0].status == "az returned no access token"
