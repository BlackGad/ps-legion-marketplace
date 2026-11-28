import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "integration" / "run.py"
SPEC = importlib.util.spec_from_file_location("integration_runner", RUNNER)
assert SPEC
assert SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_listing_parses_json_and_text_with_disabled_entries() -> None:
    json_entry = {"plugin": {"listing": {"format": "json", "idField": "id", "enabledField": "enabled"}}}
    text_entry = {"plugin": {"listing": {"format": "lines", "rejectPattern": "disabled"}}}
    assert runner.listed(json_entry, '[{"id":"legion@ps-legion-marketplace","enabled":false}]', include_disabled=True)
    assert not runner.listed(json_entry, '[{"id":"legion@ps-legion-marketplace","enabled":false}]')
    assert runner.listed(text_entry, "legion@ps-legion-marketplace 0.16.0")
    assert not runner.listed(text_entry, "legion@ps-legion-marketplace disabled")


def test_listing_compares_the_whole_plugin_id_on_text_rows() -> None:
    entry = {"plugin": {"listing": {"format": "lines", "rejectPattern": "disabled"}}}
    assert not runner.listed(entry, "extra-legion@ps-legion-marketplace 1.0.0")
    assert not runner.listed(entry, "  • other@marketplace (v1.0.0)\n      from /x/legion@ps-legion-marketplace")
    assert runner.listed(entry, "  • legion@ps-legion-marketplace (v1.0.0) (enabled)\n      from /x/checkout")


@pytest.mark.parametrize(("current", "expected"), [("0.17.0", "0.17.1"), ("0.17.1-rc.1+build", "0.17.2")])
def test_stage_candidate_commits_distinct_versions_only_in_the_fixture(tmp_path: Path, current: str, expected: str) -> None:
    instance = make_runner(tmp_path)
    source = instance.new_repo("source")
    instance.checkout = source
    manifests = ["plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"]
    for name in manifests:
        path = source / "plugins/legion" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": "legion", "version": current}), encoding="utf-8")
    lockfile = "plugins/legion/uv.lock"
    (source / lockfile).write_text("original lock\n", encoding="utf-8")
    instance.run(["git", "-C", str(source), "add", "."])
    instance.run(["git", "-C", str(source), "commit", "--quiet", "-m", "add manifests"])

    source_head = instance.run(["git", "-C", str(source), "rev-parse", "HEAD"])
    candidate = instance.work / "candidate"
    instance.run(["git", "clone", "--quiet", str(source), str(candidate)])
    release = instance.run(["git", "-C", str(candidate), "rev-parse", "HEAD"])
    run_git = instance.run
    lock_calls = []

    def run(argv: list[str]) -> str:
        if argv[0] == "git":
            return run_git(argv)
        lock_calls.append(argv)
        (candidate / lockfile).write_text("candidate lock\n", encoding="utf-8")
        return ""

    instance.run = run
    assert instance.stage_candidate(candidate, current, "8.2.1") == expected
    assert lock_calls == [[str(instance.uv), "lock", "--project", str(candidate / "plugins/legion"), "--upgrade-package", "click<8.2.1"]]
    assert instance.run(["git", "-C", str(candidate), "rev-parse", "HEAD^"]) == release
    for name in manifests:
        relative = f"plugins/legion/{name}"
        assert json.loads(instance.run(["git", "-C", str(candidate), "show", f"HEAD:{relative}"]))["version"] == expected
        assert json.loads((source / relative).read_text(encoding="utf-8"))["version"] == current
    assert instance.run(["git", "-C", str(candidate), "show", f"HEAD:{lockfile}"]) == "candidate lock"
    assert (source / lockfile).read_text(encoding="utf-8") == "original lock\n"
    assert instance.run(["git", "-C", str(source), "rev-parse", "HEAD"]) == source_head
    assert not instance.run(["git", "-C", str(source), "status", "--porcelain"])


def test_report_writes_all_evidence_and_omits_failed_benchmark(tmp_path: Path) -> None:
    cases = [runner.Case("install", False, "passed", 1.25, "", "ok"), runner.Case("install", True, "failed", 0.5, "repeat failed", "failure")]
    runner.write_report(tmp_path, "linux", cases, ["git: 2.0"], "claude 1.0")
    assert (tmp_path / "integration-linux.xml").exists()
    assert 'name="install (repeat)"' in (tmp_path / "integration-linux.xml").read_text(encoding="utf-8")
    assert [entry["name"] for entry in json.loads((tmp_path / "integration-bench-linux.json").read_text(encoding="utf-8"))] == ["linux / install"]
    assert "| install | 1.250 | 0.500 | failed |" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_pwsh_trap_records_an_attempt(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    with pytest.raises(AssertionError, match="a command called pwsh"), instance.without_pwsh():
        subprocess.run([sys.executable, "-c", "import shutil, subprocess; subprocess.run([shutil.which('pwsh')], check=False)"], env=instance.env, capture_output=True, check=False)
    assert (instance.work / "pwsh-trap/called").exists()


def make_runner(tmp_path: Path) -> Any:
    uv = tmp_path / "fixture-uv"
    uv.write_bytes(b"fixture uv")
    uv.chmod(0o755)
    return runner.Runner(Path(__file__).resolve().parents[2], tmp_path / "home", tmp_path / "results", False, uv=str(uv))


def test_nested_runners_isolate_uv_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inherited = tmp_path / "inherited"
    uv_folders = {"UV_TOOL_DIR": "uv-tools", "UV_TOOL_BIN_DIR": ".local/bin", "UV_CACHE_DIR": "uv-cache"}
    for variable in (*uv_folders, "UV_INSTALL_DIR", "XDG_BIN_HOME"):
        monkeypatch.setenv(variable, str(inherited))
    parent = make_runner(tmp_path)
    children = [parent.nested(parent.checkout, parent.work / name / "home") for name in ("update", "knowledge")]

    for instance in [parent, *children]:
        for variable, relative in uv_folders.items():
            assert instance.env[variable] == str(instance.home / relative)
        assert instance.uv.parent == instance.home / ".local/bin"
        assert instance.tool_dir == instance.home / "uv-tools"
        assert Path(instance.command("uv")) == instance.uv
        assert instance.env["UV_PYTHON_DOWNLOADS"] == "never"
        assert "UV_INSTALL_DIR" not in instance.env
        assert "XDG_BIN_HOME" not in instance.env
        assert instance.uv.read_bytes() == b"fixture uv"

    for child in children:
        assert child.env["PATH"].split(os.pathsep) == [str(child.uv.parent), *parent.client_path.split(os.pathsep)]
    children[0].uv.write_bytes(b"changed copy")
    assert parent.uv.read_bytes() == children[1].uv.read_bytes() == b"fixture uv"
    assert not inherited.exists()


def test_nested_runner_finds_clients_installed_in_parent_uv_bin(tmp_path: Path) -> None:
    parent = make_runner(tmp_path)
    claude = parent.uv.with_name("claude.exe" if runner.WINDOWS else "claude")
    claude.touch(mode=0o755)
    parent.versions["claude"] = ("1.2.3", parent.command("claude"))

    child = parent.nested(parent.checkout, parent.work / "update" / "home")

    assert Path(child.command("claude")) == claude
    assert Path(child.command("uv")) == child.uv


def test_wait_tool_version_waits_for_the_handover_to_clear_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_runner(tmp_path)
    executable = instance.uv.with_name("legion.exe" if runner.WINDOWS else "legion")
    executable.touch()
    receipt = instance.tool_dir / "legion/uv-receipt.toml"
    receipt.parent.mkdir(parents=True)
    receipt.touch()
    stop = instance.home / ".legion/run/stop"
    stop.parent.mkdir(parents=True)
    stop.touch()
    instance.tool_version = Mock(return_value="1.2.4")
    instance.legion_executable = Mock(return_value=executable)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: stop.unlink())

    instance.wait_tool_version("1.2.4")

    assert not stop.exists()


def test_report_accepts_empty_and_control_character_messages(tmp_path: Path) -> None:
    runner.write_report(tmp_path, "linux", [runner.Case("x", False, "failed", 0, "\x1b[31mwrong\x08\x1b[0m", ""), runner.Case("x", True, "skipped", 0, "", "")], [])
    xml = (tmp_path / "integration-linux.xml").read_text(encoding="utf-8")
    assert 'message="wrong"' in xml
    assert "\x08" not in xml
    assert "\x1b" not in xml
    assert "skipped:" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_command_path_survives_trap(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    folder = instance.uv.parent
    (folder / ("vendor.cmd" if os.name == "nt" else "vendor")).write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        (folder / "vendor").chmod(0o755)
    with instance.without_pwsh():
        assert Path(instance.command("vendor")).parent == folder
    with instance.without_pwsh():
        assert Path(instance.command("vendor")).parent == folder


@pytest.mark.parametrize(("new_version", "new_command"), [("2.0.0", "old"), ("1.0.0", "new")])
def test_repeat_version_reports_both_values(tmp_path: Path, new_version: str, new_command: str) -> None:
    instance = make_runner(tmp_path)
    instance.versions["claude"] = ("1.0.0", "old")
    instance.install_tool = lambda _tool: "skip: tool (already done)"
    instance.command = lambda _name: new_command
    instance.run = lambda _argv, **_kwargs: new_version
    with pytest.raises(AssertionError, match=f"{new_version}.*{new_command}"):
        instance.install_cli("claude", True)


def test_repeat_snapshot_reports_omitted_count() -> None:
    with pytest.raises(AssertionError, match="5 more paths omitted"):
        runner.unchanged({}, {str(i): "new" for i in range(25)})


def test_windows_zip_retry_uses_exact_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_runner(tmp_path)
    monkeypatch.setattr(runner, "WINDOWS", True)
    calls = []

    def fail(*args: str) -> str:
        calls.append(args)
        raise RuntimeError("uv exited 20")

    instance.legion = fail
    with pytest.raises(RuntimeError, match="exited 20"):
        instance.install_tool("uv")
    assert len(calls) == 1


def test_steps_keep_first_repeat_order(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    names = [name for name, _ in instance.steps()]
    assert names == [
        "bootstrap",
        "install-cli-claude",
        "install-cli-codex",
        "install-cli-copilot",
        "install-legion",
        "update-legion",
        "setup",
        "add",
        "remove",
        "repo-private",
        "repo-shared",
        "channel",
        "foundry-launch",
        "no-pwsh",
        "uninstall",
        "reinstall",
        "update-uv-tool",
        "purge",
        "knowledge-setup",
        "knowledge-uninstall",
        "knowledge-declined",
        "knowledge-off",
        "knowledge-purge",
    ]


@pytest.mark.parametrize(("step", "choice"), [("declined", "--no-knowledge"), ("off", "--knowledge")])
def test_knowledge_setup_restores_checkout_after_uninstall(tmp_path: Path, step: str, choice: str) -> None:
    instance = make_runner(tmp_path)
    accounts = [(tool, instance.home / ".legion/agents" / f"wiki-{tool}") for tool in runner.TOOLS]
    instance.wiki_accounts = lambda: accounts
    installed = set()

    def legion(*args: str) -> str:
        if args[0] == "install":
            assert args == ("install", "--source", str(instance.checkout), "--ref", "release", "--yes")
            installed.update(accounts)
        if args == ("setup", choice, "--yes"):
            assert installed == set(accounts), "setup would install missing Legion copies from GitHub"
        return json.dumps({"knowledge": {"state": "declined"}}) if args[0] == "status" else ""

    instance.legion = legion
    instance.listing = lambda tool, folder: (tool, folder) in installed
    for name in ("manual_wiki", "check_wiki", "check_wiki_removed", "check_uninstalled", "check_tool_removed"):
        setattr(instance, name, Mock())
    (instance.home / ".legion/knowledge").mkdir(parents=True)
    pointer = instance.home / ".config/llm-wiki/config.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text('{"hub_path":"~/.legion/knowledge"}', encoding="utf-8")

    getattr(instance, f"knowledge_{step}")(False)


def test_update_first_reinstalls_a_tool_then_repeat_is_stable(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    expected = json.loads((instance.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
    calls = []
    owner_calls = []
    instance.active = lambda: ["codex"]
    instance.targets["codex"] = (str(instance.checkout), "release")
    instance.cli = lambda tool, args, _folder=None: calls.append((tool, tuple(args))) or ""
    instance.listing = lambda *_args, **_kwargs: False

    def legion(*args: str, **kwargs: bool) -> str:
        calls.append((args, kwargs))
        owner_calls.append(args)
        if len(owner_calls) == 1:
            return f"Update legion for codex\nResults:\ncodex  {instance.folder('codex')}  installed legion {expected}"
        return "Nothing to do. Legion is already up to date."

    instance.legion = legion
    instance.version = lambda _tool, _folder: expected
    instance.installed_source_matches = lambda _tool, _folder: True
    instance.home_snapshot = Mock(return_value={"settings": "same"})
    instance.check_tool = Mock()
    instance.update(False)
    instance.update(True)
    assert calls[:2] == [("codex", tuple(instance.catalog["codex"]["plugin"][name])) for name in ("uninstall", "marketplaceRemove")]
    assert calls[2:] == [(("update", "--source", str(instance.checkout), "--yes"), {"owner": True}), (("update", "--yes"), {"owner": True})]
    assert instance.home_snapshot.call_args_list == [call(update=True), call(update=True)]
    assert instance.check_tool.call_args_list == [call(expected), call(expected)]


def test_update_rejects_a_release_instead_of_the_checkout(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    expected = json.loads((instance.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
    instance.active = lambda: ["codex"]
    instance.targets["codex"] = (str(instance.checkout), "release")
    instance.cli = lambda *_args, **_kwargs: ""
    instance.listing = lambda *_args, **_kwargs: False
    instance.legion = lambda *_args, **_kwargs: f"Update legion for codex\nResults:\ncodex  {instance.folder('codex')}  installed legion {expected}"
    instance.version = lambda _tool, _folder: expected
    instance.installed_source_matches = lambda _tool, _folder: False
    with pytest.raises(AssertionError, match="update did not keep the PR checkout source"):
        instance.update(False)


def test_published_source_repeat_uses_documented_owner_command(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    expected = json.loads((instance.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
    instance.active = lambda: ["claude"]
    instance.targets["claude"] = (runner.MARKETPLACE, "main")
    instance.cli = Mock(return_value="")
    instance.listing = lambda *_args, **_kwargs: False
    calls = []
    instance.legion = lambda *args, **kwargs: (
        calls.append((args, kwargs))
        or (
            f"Update legion for claude\nResults:\nclaude  {instance.folder('claude')}  installed legion {expected}"
            if len(calls) == 1
            else f"Update legion for claude\nskip: legion {expected} (already done)"
        )
    )
    instance.version = lambda _tool, _folder: expected
    instance.installed_source_matches = lambda _tool, _folder: True
    instance.home_snapshot = Mock(return_value={"settings": "same"})
    instance.check_tool = Mock()
    instance.update(False)
    instance.update(True)
    assert calls == [(("update", "--source", runner.MARKETPLACE, "--ref", "main", "--yes"), {"owner": True}), (("update", "--yes"), {"owner": True})]
    assert instance.cli.call_args_list == [call("claude", instance.catalog["claude"]["plugin"][name]) for name in ("uninstall", "marketplaceRemove")]
    assert instance.home_snapshot.call_args_list == [call(update=True), call(update=True)]
    assert instance.check_tool.call_args_list == [call(expected), call(expected)]


def test_update_uv_tool_repeats_on_the_upgraded_fixture_then_purges(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    root = instance.work / "p3"
    release = root / "release"
    plugin = release / "plugins/legion"
    current, candidate = "1.2.3", "1.2.4"
    initial_pin, candidate_pin = "8.2.1", "8.2.0"
    status = {"settings": {"path": str(root / "home/.legion/configuration/personalisation.json")}}
    clone = ["git", "clone", "--quiet", "--no-local", "--depth", "1", "--config", "core.longpaths=true", str(instance.checkout), str(release)]
    branch = ["git", "-C", str(release), "switch", "--quiet", "-C", "stable"]

    def run_git(argv: list[str]) -> str:
        if argv == clone:
            plugin.mkdir(parents=True)
            (plugin / "plugin.json").write_text(json.dumps({"version": current}), encoding="utf-8")
            shutil.copyfile(instance.checkout / "plugins/legion/tools.json", plugin / "tools.json")
        else:
            assert argv == branch
        return ""

    instance.run = Mock(side_effect=run_git)
    nested = instance.nested
    children = []

    def manifests(tool: str, _folder: Path) -> list[Path]:
        manifest = root / tool / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"version": candidate}), encoding="utf-8")
        return [manifest]

    def prepare_child(checkout: Path, home: Path) -> Any:
        child = nested(checkout, home)
        child.click_pin = Mock(side_effect=[initial_pin, candidate_pin])
        child.installed_click = Mock(side_effect=[initial_pin, candidate_pin])
        child.install_legion = Mock()
        child.cli = Mock()
        child.version = Mock(side_effect=[current] * len(runner.TOOLS) + [candidate] * len(runner.TOOLS))
        child.stage_candidate = Mock(return_value=candidate)
        child.legion = Mock(side_effect=[f"legion {current} -> {candidate}", json.dumps(status), f"skip: legion command {candidate} (already done)", "removed"])
        child.uv_tool_snapshot = Mock(return_value={"state": "candidate"})
        child.check_tool = Mock()
        child.wait_tool_version = Mock()
        child.legion_executable = Mock(return_value=child.uv.with_name("legion.exe" if runner.WINDOWS else "legion"))
        child.plugin_manifests = Mock(side_effect=manifests)
        child.run = Mock(side_effect=[json.dumps(status)] + [json.dumps({"additionalContext": "you are a legion member"})] * len(runner.TOOLS))
        child.check_codex_mcp = Mock()
        child.check_tool_removed = Mock()
        child.check_uninstalled = Mock()
        child.listing = Mock(return_value=False)
        children.append(child)
        return child

    instance.nested = Mock(side_effect=prepare_child)
    instance.update_uv_tool(False)
    children[0].check_tool_removed.assert_not_called()
    instance.update_uv_tool(True)

    [child] = children
    assert instance.run.call_args_list == [call(clone), call(branch)]
    instance.nested.assert_called_once_with(release, root / "home")
    child.cli.assert_called_once_with("copilot", [word.replace("{source}", release.as_posix()) for word in child.catalog["copilot"]["plugin"]["marketplaceAdd"]])
    child.install_legion.assert_called_once_with(runner.MARKETPLACE)
    assert child.version.call_args_list == [call(tool, child.folder(tool)) for tool in runner.TOOLS] * 2
    child.stage_candidate.assert_called_once_with(release, current, initial_pin)
    assert child.check_tool.call_args_list == [call(current), call(candidate)]
    child.wait_tool_version.assert_called_once_with(candidate)
    assert child.installed_click.call_count == 2
    update = call("update", "--yes", owner=True)
    assert child.legion.call_args_list == [update, call("status", "--json", owner=True), update, call("uninstall", "--purge", "--yes", owner=True)]
    assert child.uv_tool_snapshot.call_count == 2
    assert child.run.call_args_list[0].args[0][-1] == str(child.legion_executable.return_value)
    assert [entry.args[0][-3] for entry in child.run.call_args_list[1:]] == [str(root / tool / "hooks/session_start.py") for tool in runner.TOOLS]
    child.check_codex_mcp.assert_called_once_with()
    child.check_tool_removed.assert_called_once_with()
    assert child.check_uninstalled.call_args_list == [call(tool, child.folder(tool)) for tool in runner.TOOLS]


def test_installed_source_check_rejects_different_plugin_bytes(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    root = instance.folder("codex") / instance.catalog["codex"]["plugin"]["installedRoot"] / "1.2.3"
    source = root / "hooks/session_start.py"
    source.parent.mkdir(parents=True)
    (root / "plugin.json").write_text('{"name":"legion","version":"1.2.3"}', encoding="utf-8")
    source.write_bytes((instance.checkout / "plugins/legion/hooks/session_start.py").read_bytes())
    for metadata in (".codex-plugin", ".claude-plugin"):
        sibling = root / metadata
        sibling.mkdir()
        (sibling / "plugin.json").write_text('{"name":"legion"}', encoding="utf-8")
    assert instance.installed_source_matches("codex", instance.folder("codex"))
    source.write_text("release copy", encoding="utf-8")
    assert not instance.installed_source_matches("codex", instance.folder("codex"))


def test_no_ci_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(sys, "argv", [str(RUNNER), "--out", str(tmp_path / "out"), "--home-root", str(tmp_path / "home")])
    with pytest.raises(SystemExit):
        runner.main()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("tool", "runtime"), [("codex", "tmp/arg0-linux/codex-arg0-test/file"), ("copilot", "logs/process-123-456.log")])
def test_snapshot_excludes_cli_runtime_files(tmp_path: Path, tool: str, runtime: str) -> None:
    instance = make_runner(tmp_path)
    path = instance.folder(tool) / runtime
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("one", encoding="utf-8")
    first = instance.home_snapshot()
    path.write_text("two", encoding="utf-8")
    assert instance.home_snapshot() == first


@pytest.mark.parametrize(("tool", "runtime"), [("codex", "tmp/arg0-linux/codex-arg0-test/apply_patch"), ("copilot", "logs/process-123-456.log")])
def test_snapshot_excludes_account_runtime_but_detects_account_changes(tmp_path: Path, tool: str, runtime: str) -> None:
    instance = make_runner(tmp_path)
    account = instance.home / ".legion/agents" / f"{tool}1"
    settings = instance.home / ".legion/configuration/personalisation.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"agents": [{"id": f"{tool}1", "tool": tool, "configDir": str(account)}]}), encoding="utf-8")
    runtime_path = account / runtime
    stable = account / "config.toml"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("one", encoding="utf-8")
    stable.write_text("one", encoding="utf-8")
    first = instance.home_snapshot()
    runtime_path.write_text("two", encoding="utf-8")
    assert instance.home_snapshot() == first
    stable.write_text("two", encoding="utf-8")
    with pytest.raises(AssertionError, match=rf"changed: .legion/agents/{tool}1/config.toml"):
        runner.unchanged(first, instance.home_snapshot())


@pytest.mark.parametrize("purge", [False, True], ids=["uninstall", "purge"])
def test_full_uninstall_uses_owner_then_project_command_and_keeps_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, purge: bool) -> None:
    instance = make_runner(tmp_path)
    legion_home = instance.home / ".legion"
    for name in ("configuration", "agents"):
        (legion_home / name).mkdir(parents=True)
    executable = instance.uv.with_name("legion.exe" if runner.WINDOWS else "legion")
    executable.touch()
    environment = instance.tool_dir / "legion"
    environment.mkdir(parents=True)
    instance.targets_for_uninstall = Mock(return_value=[])
    instance.active = list
    instance.cli = Mock(return_value="")
    args = ["uninstall", *(["--purge"] if purge else []), "--yes"]
    uv = str(instance.uv)
    owner_command = [str(executable), *args]
    project_command = [uv, "run", "--project", str(instance.checkout / "plugins/legion"), "--frozen", "--quiet", "legion", "--home", str(instance.home), *args]

    def run(argv: list[str], *, env: dict[str, str] | None = None, answers: str | None = None) -> str:
        assert answers is None
        assert Path(argv[0]).is_file()
        if argv == owner_command:
            executable.unlink()
            environment.rmdir()
            if purge:
                shutil.rmtree(legion_home)
            return "removed"
        if argv == project_command:
            assert env is not None
            assert env["UV_PROJECT_ENVIRONMENT"] == str(instance.work / "python-env")
            assert env["PATH"].split(os.pathsep)[0] == str(instance.uv.parent)
            return f"skip: {'purge' if purge else 'uninstall'} (already done)"
        if argv == [uv, "tool", "dir", "--bin"]:
            return str(instance.uv.parent)
        if argv == [uv, "tool", "list"]:
            return ""
        if argv == [uv, "--version"]:
            return "uv fixture version"
        pytest.fail(f"unexpected command: {argv}")

    instance.run = Mock(side_effect=run)
    wait_gone = Mock(wraps=runner.wait_gone)
    monkeypatch.setattr(runner, "wait_gone", wait_gone)
    action = instance.purge if purge else instance.uninstall
    assert action(False) == "removed"
    assert action(True) == f"skip: {'purge' if purge else 'uninstall'} (already done)"
    tool_check = [[uv, "tool", "dir", "--bin"], [uv, "tool", "list"], [uv, "--version"]]
    assert [entry.args[0] for entry in instance.run.call_args_list] == [tool_check[0], owner_command, *tool_check, project_command, *tool_check]
    removed_paths = [call(executable, environment)]
    assert wait_gone.call_args_list == ([call(legion_home), *removed_paths] if purge else removed_paths) * 2
    assert instance.targets_for_uninstall.call_args_list == ([call(all_tools=True)] if purge else [call()])
    assert instance.uv.read_bytes() == b"fixture uv"
    assert not executable.exists()
    assert not environment.exists()
    assert legion_home.is_dir() is not purge


@pytest.mark.parametrize("uv_works", [False, True], ids=["broken-uv", "legion-still-listed"])
def test_full_uninstall_rejects_invalid_uv_state(tmp_path: Path, uv_works: bool) -> None:
    instance = make_runner(tmp_path)
    instance.legion = Mock(return_value="skip: uninstall (already done)")

    def run(argv: list[str]) -> str:
        assert argv[0] == str(instance.uv)
        if argv[1:] == ["tool", "dir", "--bin"]:
            return str(instance.uv.parent)
        if argv[1:] == ["tool", "list"]:
            return "legion v1.2.3" if uv_works else ""
        assert argv[1:] == ["--version"]
        raise RuntimeError("fixture uv cannot run")

    instance.run = run
    if uv_works:
        with pytest.raises(AssertionError, match="uv still lists legion after uninstall"):
            instance.uninstall(True)
    else:
        with pytest.raises(RuntimeError, match="uv cannot run"):
            instance.uninstall(True)


@pytest.mark.parametrize("text", ["codex exited 1\nlocal directory source is not supported", "claude exited 1\nlocal directory source is not supported"])
def test_local_refusal_only_skips_its_cli(tmp_path: Path, text: str) -> None:
    instance = make_runner(tmp_path)

    def install(_source: str) -> str:
        raise RuntimeError("Results:\ncodex  /cfg  failed: " + text.replace("\n", " "))

    instance.install_legion = install
    instance.listing = lambda _tool, folder, *_args, **_kwargs: not str(folder).startswith(str(instance.work / "control-"))
    expected = json.loads((instance.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
    instance.version = Mock(return_value=expected)
    instance.check_tool = Mock()
    instance.legion = Mock(return_value="")
    instance.home_snapshot = dict
    if text.startswith("codex"):
        instance.install_first()
        assert list(instance.not_tested) == ["codex"]
        assert instance.version.call_args_list == [call(tool, instance.folder(tool)) for tool in runner.TOOLS if tool != "codex"]
        instance.check_tool.assert_called_once_with(expected)
    else:
        with pytest.raises(AssertionError, match=r"codex .*claude exited"):
            instance.install_first()
        assert not instance.not_tested


@pytest.mark.parametrize("moved", [False, True])
def test_source_fallback_rejects_other_or_moving_main(tmp_path: Path, moved: bool) -> None:
    instance = make_runner(tmp_path)
    instance.fallback = True

    def install(source: str, _ref: str = "release") -> str:
        if source == str(instance.checkout):
            raise RuntimeError("Results:\ncodex  /cfg  failed: codex exited 1 local directory source is not supported")
        return "installed"

    instance.install_legion = install
    hashes = iter(["a", "a" if moved else "b", "b"])
    instance.run = lambda _argv, **_kwargs: next(hashes)
    with pytest.raises(AssertionError, match=r"needs main to match|main moved"):
        instance.install_first()


def test_purge_rejects_a_leftover_marketplace(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    instance.legion = lambda *_args, **_kwargs: "skip: purge (already done)"
    instance.check_tool_removed = Mock()
    instance.listing = lambda _tool, _folder, kind="plugin", **_kwargs: kind == "marketplace"
    instance.cli = lambda *_args: ""
    folder = instance.folder("copilot")
    folder.mkdir(parents=True)
    instance.state["purge_targets"] = [("copilot", folder)]
    with pytest.raises(AssertionError, match="marketplace remains"):
        instance.purge(True)


def test_version_orders_semantic_parts(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    root = instance.folder("claude") / instance.catalog["claude"]["plugin"]["installedRoot"]
    for name in ("0.9.0", "0.10.0"):
        path = root / name / "plugin.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": "legion", "version": name}), encoding="utf-8")
    assert instance.version("claude", instance.folder("claude")) == "0.10.0"


@pytest.mark.parametrize("kind", ["disabled", "remote"])
def test_copilot_version_requires_enabled_local_marketplace(tmp_path: Path, kind: str) -> None:
    instance = make_runner(tmp_path)
    folder = instance.folder("copilot")
    checkout = instance.checkout
    folder.mkdir(parents=True)
    (folder / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {"legion@ps-legion-marketplace": kind != "disabled"},
                "extraKnownMarketplaces": {"ps-legion-marketplace": {"source": {"source": "remote" if kind == "remote" else "directory", "path": str(checkout)}}},
            }
        ),
        encoding="utf-8",
    )
    assert instance.version("copilot", folder) is None


def test_copilot_published_fallback_reads_installed_manifest(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    folder = instance.folder("copilot")
    folder.mkdir(parents=True)
    (folder / "settings.json").write_text(
        json.dumps(
            {"enabledPlugins": {"legion@ps-legion-marketplace": True}, "extraKnownMarketplaces": {"ps-legion-marketplace": {"source": {"source": "remote", "path": "BlackGad/ps-legion-marketplace"}}}}
        ),
        encoding="utf-8",
    )
    manifest = folder / "installed-plugins/ps-legion-marketplace/legion/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"legion","version":"0.16.0"}', encoding="utf-8")
    assert instance.version("copilot", folder) == "0.16.0"


def test_listing_runs_outside_checkout(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    seen = []
    instance.command = lambda name: name
    instance.run = lambda _argv, **kwargs: seen.append(kwargs.get("cwd")) or ""
    instance.cli("claude", ["plugin", "list"])
    assert seen == [instance.work]


def test_private_repeat_needs_exact_skip(tmp_path: Path) -> None:
    instance = make_runner(tmp_path)
    repo = instance.work / "repo-a"
    repo.mkdir()
    for name in ("configuration/project.json", "configuration/workflow.md", "scripts/index.md", "scripts/check-task.ps1", "state/handoff.md"):
        path = repo / ".legion" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    instance.legion = lambda *_args, **_kwargs: "skip: unrelated"
    instance.commit_count = lambda _path: 1
    instance.run = lambda *_args, **_kwargs: ""
    instance.state["inner_commits"] = 1
    with pytest.raises(AssertionError, match="Private orchestration"):
        instance.repository("private", True)


@pytest.mark.skipif(sys.platform != "win32", reason="winget changes the Windows saved PATH")
def test_command_finds_winget_alias_on_saved_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_runner(tmp_path)
    folder = tmp_path / "winget-links"
    folder.mkdir()
    (folder / "claude.cmd").write_text("@echo off\r\necho fixture-version\r\n", encoding="utf-8")
    instance.env["PATH"] = str(tmp_path / "empty")
    monkeypatch.setattr(runner, "windows_saved_path_folders", lambda: [folder])
    assert Path(instance.command("claude")).parent == folder


MARKETPLACES = ".claude/plugins/known_marketplaces.json"


def marketplaces(source: str = "/repo", stamp: str = "2026-11-25T11:42:18.839Z", **extra: Any) -> str:
    entry = {"source": {"source": "directory", "path": source}, "installLocation": source, "lastUpdated": stamp, **extra}
    return json.dumps({"ps-legion-marketplace": entry, "other": {"source": {"source": "github"}, "lastUpdated": "a"}})


def update_snapshot(tmp_path: Path, content: str) -> dict[str, str]:
    instance = make_runner(tmp_path)
    path = instance.home / MARKETPLACES
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return instance.home_snapshot(update=True)


def test_update_snapshot_ignores_only_the_marketplace_last_updated_timestamp(tmp_path: Path) -> None:
    first = update_snapshot(tmp_path, marketplaces())
    assert update_snapshot(tmp_path, marketplaces(stamp="2026-11-25T11:43:00.000Z")) == first
    # The original snapshot keeps seeing the timestamp.
    instance = make_runner(tmp_path)
    assert instance.home_snapshot() != first


@pytest.mark.parametrize(
    "content",
    [
        marketplaces(source="/other"),
        marketplaces(installLocation="/elsewhere"),
        marketplaces(extra="new"),
        json.dumps({"renamed": {"source": {"source": "github"}}}),
        marketplaces().replace('"other"', '"other2"'),
    ],
)
def test_update_snapshot_still_detects_other_marketplace_changes(tmp_path: Path, content: str) -> None:
    first = update_snapshot(tmp_path, marketplaces())
    with pytest.raises(AssertionError, match=rf"changed: {MARKETPLACES}"):
        runner.unchanged(first, update_snapshot(tmp_path, content))


def test_update_snapshot_keeps_malformed_marketplaces_visible(tmp_path: Path) -> None:
    first = update_snapshot(tmp_path, "{not json")
    assert update_snapshot(tmp_path, "{not json") == first
    with pytest.raises(AssertionError, match=rf"changed: {MARKETPLACES}"):
        runner.unchanged(first, update_snapshot(tmp_path, "{not json!"))
    with pytest.raises(AssertionError, match=rf"changed: {MARKETPLACES}"):
        runner.unchanged(update_snapshot(tmp_path, marketplaces()), update_snapshot(tmp_path, "[1]"))


def test_update_snapshot_still_detects_other_claude_config_changes(tmp_path: Path) -> None:
    first = update_snapshot(tmp_path, marketplaces())
    instance = make_runner(tmp_path)
    (instance.home / ".claude/plugins/installed_plugins.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"added: .claude/plugins/installed_plugins.json"):
        runner.unchanged(first, instance.home_snapshot(update=True))


def test_codex_inventory_requires_connected_legion_with_status() -> None:
    listed = {"name": "legion", "runtimeStatus": "connected", "tools": {"status": {}, "limits": {}}}
    assert runner.codex_legion_server([{"name": "other", "runtimeStatus": "connected", "tools": {"status": {}}}, listed]) == listed
    assert runner.codex_legion_server([]) is None
    assert runner.codex_legion_server([{**listed, "tools": {"limits": {}}}]) is None
    assert runner.codex_legion_server([{**listed, "runtimeStatus": "failed"}]) is None


FAKE_APP_SERVER = """
import json, sys
lists = 0
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "thread/start":
        result = {"thread": {"id": "t1"}}
    elif method == "mcpServerStatus/list":
        assert message["params"] == {"threadId": "t1", "serverName": "legion"}
        lists += 1
        listed = [{"name": "legion", "runtimeStatus": "connected", "tools": {"status": {}}}]
        result = {"data": listed if lists >= LISTS_BEFORE_LEGION else []}
    else:
        result = {}
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
"""


def probe(tmp_path: Path, script: str, seconds: float) -> list[dict[str, Any]]:
    return runner.probe_codex_mcp([sys.executable, "-c", script], os.environ.copy(), tmp_path, seconds)


def test_codex_probe_asks_for_legion_and_returns_its_status(tmp_path: Path) -> None:
    servers = probe(tmp_path, "LISTS_BEFORE_LEGION = 1" + FAKE_APP_SERVER, 30)
    assert runner.codex_legion_server(servers) == servers[0]


def test_codex_probe_fails_when_app_server_exits(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="closed its output"):
        probe(tmp_path, "", 30)


def test_codex_probe_fails_on_unreadable_output(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="JSONDecodeError"):
        probe(tmp_path, "print('not json', flush=True); import time; time.sleep(60)", 30)


def pid_exists(pid: int) -> bool:
    if os.name == "nt":
        listing = subprocess.run([shutil.which("tasklist") or "tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False).stdout
        return str(pid) in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_codex_probe_stops_at_one_deadline_and_ends_the_process_tree(tmp_path: Path) -> None:
    # The fake ignores closed stdin and keeps a child alive, like an npm shim around a native server.
    pid_file = tmp_path / "child.pid"
    script = f"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open({str(pid_file)!r}, "w").write(str(child.pid))
for line in sys.stdin:
    print('{{"method": "noise"}}', flush=True)
time.sleep(120)
"""
    start = time.monotonic()
    with pytest.raises(AssertionError, match="timed out"):
        probe(tmp_path, script, 2)
    assert time.monotonic() - start < 40
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(50):
        if not pid_exists(child_pid):
            break
        time.sleep(0.1)
    assert not pid_exists(child_pid)


def test_codex_probe_retries_until_legion_appears(tmp_path: Path) -> None:
    assert runner.codex_legion_server(probe(tmp_path, "LISTS_BEFORE_LEGION = 3" + FAKE_APP_SERVER, 30)) is not None


@pytest.mark.parametrize("bullet", ["•", "ΓÇó", "?", "*", ""])
def test_listing_ignores_whatever_bullet_the_cli_prints(bullet: str) -> None:
    entry = {"plugin": {"listing": {"format": "lines", "rejectPattern": "disabled"}}}
    prefix = f"  {bullet} " if bullet else "  "
    assert runner.listed(entry, f"{prefix}legion@ps-legion-marketplace (v1.0.0) (enabled)\n      from /x/checkout")
    assert not runner.listed(entry, f"{prefix}other@marketplace (v1.0.0)\n      from /x/legion@ps-legion-marketplace")
    assert not runner.listed(entry, f"{prefix}extra-legion@ps-legion-marketplace (v1.0.0)")
    assert not runner.listed(entry, f"{prefix}legion@ps-legion-marketplace-extra (v1.0.0)")
