# Exercise the lifecycle through Click, executable CLIs, and the files they leave.

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest
from click.testing import CliRunner, Result

from fake_cli import (
    CONFIG,
    FAKE_SESSION,
    PLUGIN,
    plugin_root,
    write_shim,
)
from launcher_fakes import only_on_path
from legion.cli import main

REPO = Path(__file__).resolve().parents[3]
TEST_GIT = shutil.which("git")
WAITING = "Waiting for running Legion sessions to exit..."
REMOVAL = "legion command removal scheduled." if sys.platform == "win32" else "legion command removed."
PURGE = "legion command removal scheduled, followed by Legion data removal." if sys.platform == "win32" else "legion command removed and Legion data purged."
NO_CLI = "No coding CLI is installed. Run legion setup to install one."
QUESTION = "Apply? [y/N]"
UPDATED = "987.6.5"


def read_json(file: Path) -> dict[str, Any]:
    return json.loads(file.read_text(encoding="utf-8"))


def calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def ordered(output: str, *parts: str) -> None:
    remaining = output
    for part in parts:
        assert part in remaining, f"Missing {part!r} in order in:\n{output}"
        remaining = remaining.split(part, 1)[1]


@dataclass
class Lifecycle:
    home: Path
    bin_dir: Path
    source: Path
    uv: Path
    version: str

    @property
    def settings(self) -> Path:
        return self.home / ".legion/configuration/personalisation.json"

    def enable(self, *tools: str) -> None:
        for tool in tools:
            write_shim(self.bin_dir, tool, Path(__file__).with_name("fake_cli.py"))

    def invoke(self, *args: str, answer: str | None = None, code: int = 0) -> Result:
        result = CliRunner().invoke(main, ["--home", str(self.home), *args], input=answer)
        assert result.exit_code == code, (result.output, result.exception)
        return result

    def install(self, answer: str = "y\n", code: int = 0) -> Result:
        return self.invoke("install", "--source", str(self.source), answer=answer, code=code)

    def add(self, tool: str) -> Result:
        return self.invoke("add", tool, "--source", str(self.source), "--yes")

    def outlander(self, tool: str) -> Path:
        return self.home / f".{tool}"

    def account(self, tool: str) -> Path:
        return self.home / ".legion/agents" / f"{tool}1"

    def assert_plugin(self, tool: str, folder: Path, version: str) -> None:
        assert read_json(plugin_root(tool, folder) / "plugin.json")["version"] == version
        assert read_json(folder / "fake-cli.json")["plugins"] == [PLUGIN]

    def publish_update(self) -> None:
        manifest_path = self.source / "plugins/legion/plugin.json"
        manifest = read_json(manifest_path)
        manifest["version"] = UPDATED
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def invoke_in_child(self, *args: str, answer: str, code: int = 0) -> str:
        # The handover waits for its parent to exit, so give CliRunner a short-lived process.
        command = ["--home", str(self.home), *args]
        child = subprocess.run(
            [sys.executable, str(Path(__file__)), json.dumps(command), answer],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        )
        exit_code, output = json.loads(child.stdout)
        assert exit_code == code, (output, child.stderr)
        return output

    def await_removal(self, *paths: Path) -> None:
        pending = (self.uv, self.home / ".legion/run/stop", *paths)
        deadline = time.monotonic() + 15
        while any(path.exists() for path in pending):
            if time.monotonic() > deadline:
                log = self.home / ".legion/temp/uv-tool.log"
                detail = log.read_text(encoding="utf-8") if log.exists() else ""
                pytest.fail(f"Handover left {[path for path in pending if path.exists()]}\n{detail}")
            time.sleep(0.05)


@pytest.fixture
def lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lifecycle:
    home = tmp_path.resolve() / "home with spaces"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    for name in (*CONFIG.values(), "FAKE_CLI_FAIL", "FAKE_CLI_FAIL_FOLDER", "FAKE_UV_FAIL", "LEGION_BOOTSTRAP_POWERSHELL"):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path.resolve() / "bin with spaces"
    write_shim(bin_dir, "uv", Path(__file__).with_name("fake_uv.py"))
    only_on_path(monkeypatch, bin_dir)
    uv = tmp_path / "fake-uv.json"
    monkeypatch.setenv("FAKE_UV_STATE", str(uv))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    source = tmp_path.resolve() / "checkout with spaces"
    for relative in (".claude-plugin/marketplace.json", "plugins/legion/tools.json", "plugins/legion/plugin.json"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    version = read_json(source / "plugins/legion/plugin.json")["version"]
    return Lifecycle(home, bin_dir, source, uv, version)


def test_fresh_install_without_a_cli(lifecycle: Lifecycle) -> None:
    result = lifecycle.install()
    ordered(
        result.output,
        NO_CLI,
        *(f"skip: {tool} (not installed)" for tool in CONFIG),
        "Install plan:",
        QUESTION + "\n",
        WAITING,
        f"Preparing locked dependencies for legion {lifecycle.version}...",
        f"Installing legion {lifecycle.version} with uv...",
        f"legion command {lifecycle.version} installed",
    )
    state = read_json(lifecycle.uv)
    assert state["version"] == lifecycle.version
    assert Path(state["source"]) == lifecycle.source / "plugins/legion"
    assert calls(lifecycle.uv.with_suffix(".log")).count(["tool", "update-shell"]) == int(sys.platform == "win32")
    assert not lifecycle.settings.exists()
    assert not any(lifecycle.outlander(tool).exists() for tool in CONFIG)


def test_uv_install_failure_keeps_the_plugin_copy(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle.enable("claude")
    monkeypatch.setenv("FAKE_UV_FAIL", "install")
    result = lifecycle.install(code=1)
    ordered(result.output, f"claude plugin install {PLUGIN}", WAITING, "Results:", "failed: uv tool install exited 1")
    lifecycle.assert_plugin("claude", lifecycle.outlander("claude"), lifecycle.version)
    assert not lifecycle.uv.exists()
    assert not (lifecycle.home / ".legion/run/stop").exists()


def test_fresh_install_with_clis(lifecycle: Lifecycle) -> None:
    lifecycle.enable(*CONFIG)
    result = lifecycle.install()
    ordered(
        result.output,
        "Install plan:",
        *(f"Install legion for {tool} in {lifecycle.outlander(tool)}" for tool in CONFIG),
        QUESTION + "\n",
        "claude plugin marketplace add",
        f"claude plugin install {PLUGIN}",
        f"codex plugin add {PLUGIN}",
        f"copilot plugin install {PLUGIN}",
        WAITING,
        "Results:",
        *(f"installed legion {lifecycle.version}" for _ in CONFIG),
        f"legion command {lifecycle.version} installed",
    )
    for tool in CONFIG:
        lifecycle.assert_plugin(tool, lifecycle.outlander(tool), lifecycle.version)
    assert read_json(lifecycle.uv) == {"version": lifecycle.version, "source": str(lifecycle.source / "plugins/legion")}
    assert not lifecycle.settings.exists()


def test_repeat_install_skips_existing_plugins_and_uv(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.install()
    folder = lifecycle.outlander("codex")
    files = [folder / "fake-cli.json", plugin_root("codex", folder) / "plugin.json", lifecycle.uv]
    before = [path.read_bytes() for path in files]
    cli_log, uv_log = folder / "fake-cli.log", lifecycle.uv.with_suffix(".log")
    cli_count, uv_count = len(calls(cli_log)), len(calls(uv_log))
    result = lifecycle.install()
    assert result.output.splitlines() == ["Nothing to do. Legion is already installed for the available targets."]
    assert [path.read_bytes() for path in files] == before
    assert all("list" in args for args in calls(cli_log)[cli_count:])
    assert all(args[1] in ("list", "dir") for args in calls(uv_log)[uv_count:])
    assert not lifecycle.settings.exists()


def test_unchanged_checkout_update_reports_it_is_current(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.install()
    result = lifecycle.invoke("update", "--source", str(lifecycle.source))
    assert result.output.splitlines() == ["Nothing to do. Legion is already up to date."]


def test_status_and_install_use_stock_default_folders(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle.enable("claude", "codex")
    overrides = {tool: lifecycle.home / f"override-{tool}" for tool in CONFIG}
    for tool, variable in CONFIG.items():
        monkeypatch.setenv(variable, str(overrides[tool]))
    lifecycle.add("claude")
    lifecycle.install()
    account = lifecycle.account("claude")
    claude, codex = (lifecycle.outlander(tool) for tool in ("claude", "codex"))
    logs = [folder / "fake-cli.log" for folder in (account, claude, codex)]
    before = [log.read_bytes() for log in logs]
    result = lifecycle.invoke("status")
    ordered(result.output, "accounts\n", f"claude1  claude  {account}", "outlanders\n", f"claude  {claude}  legion {lifecycle.version}", f"codex  {codex}  legion {lifecycle.version}")
    report = json.loads(lifecycle.invoke("status", "--json").output)
    assert [row["tool"] for row in report["outlanders"]] == ["claude", "codex"]
    assert [log.read_bytes() for log in logs] == before
    assert not any(folder.exists() for folder in overrides.values())
    assert not lifecycle.outlander("copilot").exists()


def test_install_repairs_unregistered_marketplace_with_cached_plugin(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    folder = lifecycle.outlander("claude")
    state_file = folder / "fake-cli.json"
    state = read_json(state_file)
    state["marketplaces"] = {}
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert f"claude  {folder}  legion {lifecycle.version}" in lifecycle.invoke("status").output
    result = lifecycle.install()
    ordered(result.output, "claude plugin marketplace add", f"claude plugin install {PLUGIN}")
    lifecycle.assert_plugin("claude", folder, lifecycle.version)


@pytest.mark.parametrize("tool", CONFIG)
def test_install_enables_a_disabled_plugin_with_a_cached_version(lifecycle: Lifecycle, tool: str) -> None:
    lifecycle.enable(tool)
    lifecycle.install()
    folder = lifecycle.outlander(tool)
    state_file = folder / "fake-cli.json"
    state = read_json(state_file)
    state["disabled"] = [PLUGIN]
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert f"{tool}  {folder}  legion {lifecycle.version}" in lifecycle.invoke("status").output
    result = lifecycle.install()
    assert f"{tool} plugin {'add' if tool == 'codex' else 'install'} {PLUGIN}" in result.output
    assert "plugin marketplace add" not in result.output
    assert read_json(state_file)["disabled"] == []


def test_account_command_off_path_does_not_create_an_outlander(lifecycle: Lifecycle) -> None:
    folder = lifecycle.account("claude")
    private_bin = lifecycle.home / "registered bin"
    write_shim(private_bin, "claude", Path(__file__).with_name("fake_cli.py"))
    [command] = private_bin.iterdir()
    lifecycle.settings.parent.mkdir(parents=True)
    settings = {"schemaVersion": 1, "agents": [{"id": "claude1", "tool": "claude", "configDir": str(folder), "command": str(command)}], "channels": []}
    lifecycle.settings.write_text(json.dumps(settings), encoding="utf-8")
    lifecycle.install()
    lifecycle.assert_plugin("claude", folder, lifecycle.version)
    report = json.loads(lifecycle.invoke("status", "--json").output)
    assert report["outlanders"] == []
    assert not lifecycle.outlander("claude").exists()


def test_uv_uses_registered_claude_copy_when_a_newer_cache_remains(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    folder = lifecycle.outlander("claude")
    active = plugin_root("claude", folder)
    stale = active / "stale/plugin.json"
    stale.parent.mkdir()
    stale.write_text('{"name":"legion","version":"999.0.0"}', encoding="utf-8")
    registration = {"plugins": {PLUGIN: [{"scope": "user", "installPath": str(active)}]}}
    (folder / "plugins/installed_plugins.json").write_text(json.dumps(registration), encoding="utf-8")
    assert f"claude  {folder}  legion {lifecycle.version}" in lifecycle.invoke("status").output
    lifecycle.uv.unlink()
    lifecycle.invoke("install", "--yes")
    assert read_json(lifecycle.uv) == {"version": lifecycle.version, "source": str(active)}


def test_outlander_keeps_a_manually_installed_wiki(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.install()
    folder = lifecycle.outlander("codex")
    wiki = "wiki@llm-wiki"
    state_file = folder / "fake-cli.json"
    state = read_json(state_file)
    state["marketplaces"]["llm-wiki"] = str(lifecycle.source)
    state["plugins"].append(wiki)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    root = plugin_root("codex", folder, wiki)
    root.mkdir(parents=True)
    (root / "plugin.json").write_text('{"name":"wiki","version":"1.0.0"}', encoding="utf-8")
    lifecycle.settings.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.settings.write_text('{"schemaVersion":1,"agents":[],"channels":[],"knowledgeEnabled":true}', encoding="utf-8")
    lifecycle.invoke("update", "--yes")
    lifecycle.invoke_in_child("uninstall", answer="y\n")
    lifecycle.await_removal()
    assert read_json(state_file) == {"marketplaces": {"llm-wiki": str(lifecycle.source)}, "plugins": [wiki]}
    upgrades = [args for args in calls(folder / "fake-cli.log") if args[:3] == ["plugin", "marketplace", "upgrade"]]
    assert not upgrades  # A current local marketplace needs no upgrade.


def test_repeat_knowledge_setup_reports_the_hub_and_pinned_wiki_already_done(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.add("claude")
    folder = lifecycle.account("claude")
    state_file = folder / "fake-cli.json"
    state = read_json(state_file)
    wiki = "wiki@llm-wiki"
    state["marketplaces"]["llm-wiki"] = {"repo": "nvk/llm-wiki", "ref": "v1.0.0"}
    state["plugins"].append(wiki)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    root = plugin_root("claude", folder, wiki)
    root.mkdir(parents=True)
    (root / "plugin.json").write_text('{"name":"wiki","version":"1.0.0"}', encoding="utf-8")

    lifecycle.invoke("setup", "--knowledge", "--yes")
    log = folder / "fake-cli.log"
    before = len(calls(log))
    output = lifecycle.invoke("setup", "--knowledge", "--yes").output
    assert f"skip: {wiki} in {folder} (already done)" in output
    rows = output.split("Results:\n", 1)[1].splitlines()
    assert any(row.startswith("knowledge hub ") and row.endswith("  already done") for row in rows)
    assert any(row.startswith("claude1 knowledge ") and row.endswith("  already done") for row in rows)
    assert all("list" in args for args in calls(log)[before:])


@pytest.mark.parametrize("answer", ["n\n", ""], ids=["no", "eof"])
def test_declined_install_leaves_home_untouched(lifecycle: Lifecycle, answer: str) -> None:
    lifecycle.enable("claude")
    before = list(lifecycle.home.iterdir())
    result = lifecycle.install(answer)
    ordered(result.output, "Install plan:", "Install legion for claude in", QUESTION, "\nNothing changed. Run again with --yes to apply.\n")
    assert list(lifecycle.home.iterdir()) == before
    assert not lifecycle.uv.exists()
    assert all(args in (["tool", "list"], ["tool", "dir", "--bin"]) for args in calls(lifecycle.uv.with_suffix(".log")))


def test_add_new_account(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    folder = lifecycle.account("claude")
    result = lifecycle.invoke("add", "claude", "--source", str(lifecycle.source), answer="y\n")
    ordered(
        result.output,
        f"Add claude1 (claude) in {folder}",
        QUESTION + "\n",
        "claude plugin marketplace add",
        f"claude plugin install {PLUGIN}",
        WAITING,
        "Results:",
        f"installed legion {lifecycle.version}",
        "log in:",
        f"legion command {lifecycle.version} installed",
    )
    assert result.output.count(QUESTION) == 1
    assert ["plugin", "marketplace", "add", lifecycle.source.as_posix()] in calls(folder / "fake-cli.log")
    [agent] = read_json(lifecycle.settings)["agents"]
    assert (agent["id"], agent["tool"], Path(agent["configDir"]).expanduser()) == ("claude1", "claude", folder)
    lifecycle.assert_plugin("claude", folder, lifecycle.version)
    assert read_json(lifecycle.uv)["version"] == lifecycle.version
    assert not lifecycle.outlander("claude").exists()


def test_add_reports_plugin_failure_and_exits_one(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle.enable("claude")
    monkeypatch.setenv("FAKE_CLI_FAIL", "plugin install")
    result = lifecycle.invoke("add", "claude", "--source", str(lifecycle.source), "--yes", code=1)
    folder = lifecycle.account("claude")
    ordered(result.output, "fake claude: plugin install failed", "Results:", "claude1", str(folder), "failed: claude exited 1", "Setup failed.")
    assert read_json(lifecycle.settings)["agents"][0]["id"] == "claude1"
    assert not (plugin_root("claude", folder) / "plugin.json").exists()
    assert not lifecycle.uv.exists()


def test_setup_with_nothing_to_do(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.add("claude")
    folder = lifecycle.account("claude")
    files = [lifecycle.settings, folder / "fake-cli.json", lifecycle.uv]
    before = [path.read_bytes() for path in files]
    result = lifecycle.invoke("setup")
    assert result.output.splitlines() == ["Nothing to do."]
    assert [path.read_bytes() for path in files] == before
    lifecycle.assert_plugin("claude", folder, lifecycle.version)


def test_plain_install_uses_the_newest_copy_once(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    claude = lifecycle.outlander("claude")
    lifecycle.install()
    lifecycle.enable("codex")
    codex = lifecycle.outlander("codex")
    codex.mkdir()
    (codex / "fake-cli.json").write_text(json.dumps({"marketplaces": {"ps-legion-marketplace": str(lifecycle.source)}, "plugins": []}), encoding="utf-8")
    lifecycle.publish_update()
    lifecycle.uv.unlink()
    log = lifecycle.uv.with_suffix(".log")
    count = len(calls(log))
    lifecycle.invoke("install", "--yes")
    lifecycle.assert_plugin("claude", claude, lifecycle.version)
    lifecycle.assert_plugin("codex", lifecycle.outlander("codex"), UPDATED)
    assert read_json(lifecycle.uv)["version"] == UPDATED
    assert [args[:2] for args in calls(log)[count:]].count(["tool", "install"]) == 1


def test_first_run_finishes_safe_steps_once(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.add("claude")
    notes = lifecycle.settings.with_name("notes.md")
    notes.unlink()
    stamp = lifecycle.home / ".legion/version"
    stamp.write_text("0.0.1\n", encoding="utf-8")
    folder = lifecycle.account("claude")
    (plugin_root("claude", folder) / "plugin.json").unlink()
    result = lifecycle.invoke("status")
    ordered(result.output, "Finish plan", "Create notes", "needs approval", "Results:", "accounts")
    assert QUESTION not in result.output
    assert notes.is_file()
    assert stamp.read_text(encoding="utf-8").strip() == lifecycle.version
    assert not (plugin_root("claude", folder) / "plugin.json").exists()
    assert "Finish plan" not in lifecycle.invoke("status").output


@pytest.mark.parametrize("args", [("validate",), ("status", "--json")])
def test_first_run_preserves_machine_output(lifecycle: Lifecycle, args: tuple[str, ...]) -> None:
    lifecycle.settings.parent.mkdir(parents=True)
    lifecycle.settings.write_text('{"schemaVersion":1,"agents":[],"channels":[]}', encoding="utf-8")
    stamp = lifecycle.home / ".legion/version"
    stamp.write_text("0.0.1\n", encoding="utf-8")
    if args[0] == "validate":
        result = lifecycle.invoke(*args, "--kind", "personalisation", "--path", str(lifecycle.settings))
        assert result.output == ""
    else:
        result = lifecycle.invoke(*args)
        assert json.loads(result.output)["accounts"] == []
    assert stamp.read_text(encoding="utf-8").strip() == "0.0.1"
    assert not lifecycle.settings.with_name("notes.md").exists()


def test_setup_decline_leaves_knowledge_and_settings_untouched(lifecycle: Lifecycle) -> None:
    result = lifecycle.invoke("setup", "--knowledge", answer="")
    ordered(result.output, "Setup plan:", "Turn knowledge on", QUESTION, "\nNothing changed. Run again with --yes to apply.\n")
    assert list(lifecycle.home.iterdir()) == []


def test_first_run_failure_retries_and_runs_the_verb(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.add("claude")
    notes = lifecycle.settings.with_name("notes.md")
    notes.unlink()
    notes.mkdir()
    stamp = lifecycle.home / ".legion/version"
    stamp.write_text("0.0.1\n", encoding="utf-8")
    result = lifecycle.invoke("status")
    assert "First-run check:" in result.stderr
    assert "accounts" in result.stdout
    assert stamp.read_text(encoding="utf-8").strip() == "0.0.1"
    notes.rmdir()
    assert "Finish plan" in lifecycle.invoke("status").output
    assert stamp.read_text(encoding="utf-8").strip() == lifecycle.version


@pytest.fixture
def installed_accounts(lifecycle: Lifecycle) -> Lifecycle:
    lifecycle.enable("claude", "codex")
    lifecycle.install()
    lifecycle.add("claude")
    lifecycle.add("codex")
    return lifecycle


def test_update_across_accounts_and_outlanders(installed_accounts: Lifecycle) -> None:
    env = installed_accounts
    env.publish_update()
    count = len(calls(env.uv.with_suffix(".log")))
    result = env.invoke("update", answer="y\n")
    targets = [(tool, folder) for tool in ("claude", "codex") for folder in (env.outlander(tool), env.account(tool))]
    ordered(
        result.output,
        "Update plan:",
        *(f"Update legion for {tool} in {folder} ({env.version})" for tool, folder in targets),
        QUESTION + "\n",
        WAITING,
        "Results:",
        *(str(folder) for _, folder in targets),
        f"legion {UPDATED} is installed; restart your sessions.",
    )
    assert result.output.count(f"{env.version} -> {UPDATED}") >= len(targets)
    for tool, folder in targets:
        env.assert_plugin(tool, folder, UPDATED)
    assert read_json(env.uv)["version"] == UPDATED
    assert [args[:2] for args in calls(env.uv.with_suffix(".log"))[count:]].count(["tool", "install"]) == 1
    assert {agent["id"] for agent in read_json(env.settings)["agents"]} == {"claude1", "codex1"}


def test_update_with_a_failed_target(installed_accounts: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> None:
    env = installed_accounts
    env.publish_update()
    monkeypatch.setenv("FAKE_CLI_FAIL", "plugin update")
    monkeypatch.setenv("FAKE_CLI_FAIL_FOLDER", str(env.account("claude")))
    result = env.invoke("update", answer="y\n", code=1)
    ordered(result.output, QUESTION + "\n", "fake claude: plugin update failed", "codex plugin marketplace upgrade", WAITING, "Results:", "failed: claude exited 1", "Update failed.")
    env.assert_plugin("claude", env.outlander("claude"), UPDATED)
    env.assert_plugin("claude", env.account("claude"), env.version)
    for folder in (env.outlander("codex"), env.account("codex")):
        env.assert_plugin("codex", folder, UPDATED)
    assert read_json(env.uv) == {"version": UPDATED, "source": str(plugin_root("claude", env.outlander("claude")))}


@pytest.mark.skipif(sys.platform != "win32", reason="Only Windows refuses updates from a registered session")
def test_windows_update_refuses_a_registered_ancestor(lifecycle: Lifecycle) -> None:
    lifecycle.install()
    run = lifecycle.home / ".legion/run"
    registration = run / f"{os.getpid()}.pid"
    registration.write_text(str(os.getpid()), encoding="ascii")
    before = lifecycle.uv.read_bytes()
    output = lifecycle.invoke_in_child("update", answer="y\n", code=1)
    assert output.splitlines() == ["Close this Legion session, run `legion update` from a terminal, then reopen Legion."]
    assert registration.is_file()
    assert lifecycle.uv.read_bytes() == before
    assert not (run / "stop").exists()


def test_uninstall_and_repeat_keep_accounts_and_settings(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.install()
    lifecycle.add("codex")
    account, outlander = lifecycle.account("codex"), lifecycle.outlander("codex")
    login = account / "login.json"
    login.write_text('{"fixture":true}', encoding="utf-8")
    settings = lifecycle.settings.read_bytes()
    result = lifecycle.invoke_in_child("uninstall", answer="y\n")
    ordered(result, "Uninstall plan:", QUESTION, WAITING, f"codex plugin remove {PLUGIN}", "Results:", REMOVAL)
    assert result.count(QUESTION) == 1
    lifecycle.await_removal()
    for folder in (account, outlander):
        assert not (plugin_root("codex", folder) / "plugin.json").exists()
        assert read_json(folder / "fake-cli.json") == {"marketplaces": {}, "plugins": []}
        ordered("\n".join(" ".join(args) for args in calls(folder / "fake-cli.log")), f"plugin remove {PLUGIN}", "plugin marketplace remove ps-legion-marketplace")
    assert lifecycle.settings.read_bytes() == settings
    assert login.read_text(encoding="utf-8") == '{"fixture":true}'
    repeat = lifecycle.invoke_in_child("uninstall", answer="")
    assert "skip: uninstall (already done)" in repeat
    assert QUESTION not in repeat
    assert calls(lifecycle.uv.with_suffix(".log")).count(["tool", "uninstall", "legion"]) == 1
    assert lifecycle.settings.read_bytes() == settings
    assert login.is_file()


@pytest.mark.parametrize("answer", ["y\n", "n\n", ""], ids=["delete-hub", "keep-hub", "hub-eof"])
def test_purge_asks_about_the_hub_separately(lifecycle: Lifecycle, answer: str) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    lifecycle.add("claude")
    legion = lifecycle.home / ".legion"
    hub = legion / "knowledge"
    hub.mkdir()
    (hub / "keep.md").write_text("hub fixture", encoding="utf-8")
    pointer = lifecycle.home / ".config/llm-wiki/config.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text('{"hub_path":"~/.legion/knowledge"}', encoding="utf-8")
    other_children = [child for child in legion.iterdir() if child != hub]
    result = lifecycle.invoke_in_child("uninstall", "--purge", answer="y\n" + answer)
    ordered(
        result,
        f"Remove folder: {legion}",
        "Asked separately: the knowledge hub",
        QUESTION,
        WAITING,
        f"claude plugin uninstall {PLUGIN}",
        "Also delete the knowledge hub in ~/.legion/knowledge? [y/N] ",
        "Results:",
        PURGE,
    )
    if answer == "y\n":
        lifecycle.await_removal(legion)
        assert not pointer.exists()
    else:
        lifecycle.await_removal(*other_children)
        assert list(legion.iterdir()) == [hub]
        assert (hub / "keep.md").read_text(encoding="utf-8") == "hub fixture"
        assert read_json(pointer) == {"hub_path": "~/.legion/knowledge"}
    outlander = lifecycle.outlander("claude")
    assert outlander.is_dir()
    assert read_json(outlander / "fake-cli.json") == {"marketplaces": {}, "plugins": []}


@pytest.mark.parametrize("answer", ["n\n", ""], ids=["no", "eof"])
def test_uninstall_decline_keeps_installations(installed_accounts: Lifecycle, answer: str) -> None:
    env = installed_accounts
    result = env.invoke("uninstall", answer=answer)
    ordered(result.output, "Uninstall plan:", QUESTION, "Nothing changed. Run again with --yes to apply.")
    assert f"\n{WAITING}\n" not in result.output
    assert env.uv.exists()
    env.assert_plugin("codex", env.account("codex"), env.version)


def test_uninstall_continues_after_a_failed_target(installed_accounts: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> None:
    env = installed_accounts
    monkeypatch.setenv("FAKE_CLI_FAIL", "plugin uninstall")
    monkeypatch.setenv("FAKE_CLI_FAIL_FOLDER", str(env.account("claude")))
    output = env.invoke_in_child("uninstall", answer="y\n", code=1)
    ordered(output, WAITING, "fake claude: plugin uninstall failed", f"codex plugin remove {PLUGIN}", "Results:", "failed: claude exited 1", "Uninstall failed.")
    env.await_removal()
    env.assert_plugin("claude", env.account("claude"), env.version)
    for folder in (env.outlander("codex"), env.account("codex")):
        assert read_json(folder / "fake-cli.json") == {"marketplaces": {}, "plugins": []}


def test_uninstall_dry_run_previews_without_removal(lifecycle: Lifecycle) -> None:
    lifecycle.enable("copilot")
    lifecycle.install()
    output = lifecycle.invoke("uninstall", "--purge", "--dry-run", answer="y\ny\n").output
    ordered(output, "Purge plan:", "Remove folder:", f"copilot plugin uninstall {PLUGIN}", "plugin marketplace remove")
    assert QUESTION not in output
    assert lifecycle.uv.is_file()
    assert not (lifecycle.home / ".legion/run/stop").exists()
    lifecycle.assert_plugin("copilot", lifecycle.outlander("copilot"), lifecycle.version)


def test_uninstall_stops_a_registered_session_before_removing_plugins(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.install()
    folder = lifecycle.outlander("codex")
    cli_log = folder / "fake-cli.log"
    run = lifecycle.home / ".legion/run"
    observed = run / "session-stopped"
    code = """
import os, sys, time
from pathlib import Path
run, cli_log, observed = map(Path, sys.argv[1:])
registration = run / f"{os.getpid()}.pid"
registration.write_text(str(os.getpid()), encoding="ascii")
while not (run / "stop").exists():
    time.sleep(0.02)
removed = any('["plugin", "remove"' in line for line in cli_log.read_text(encoding="utf-8").splitlines())
observed.write_text(str(removed), encoding="ascii")
registration.unlink()
"""
    with subprocess.Popen([sys.executable, "-c", code, str(run), str(cli_log), str(observed)]) as child:
        try:
            deadline = time.monotonic() + 10
            while not any(registration.stat().st_size for registration in run.glob("*.pid")):
                assert child.poll() is None, "Session exited before registering"
                assert time.monotonic() < deadline, "Session did not register"
                time.sleep(0.02)
            output = lifecycle.invoke_in_child("uninstall", "--yes", answer="")
            ordered(output, f"\n{WAITING}\n", f"codex plugin remove {PLUGIN}", "Results:", REMOVAL)
            assert child.wait(timeout=5) == 0
            assert observed.read_text(encoding="ascii") == "False"
            lifecycle.await_removal()
        finally:
            if child.poll() is None:
                child.kill()


def test_uninstall_stops_when_it_cannot_raise_the_stop_event(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.install()
    stop = lifecycle.home / ".legion/run/stop"
    stop.mkdir()
    result = lifecycle.invoke("uninstall", "--yes", code=1)
    ordered(result.output, f"\n{WAITING}\n", "Results:", "sessions", "failed:", "Uninstall failed.")
    assert "plugin remove" not in result.output
    lifecycle.assert_plugin("codex", lifecycle.outlander("codex"), lifecycle.version)
    assert lifecycle.uv.is_file()


def test_uninstall_removes_managed_wiki_and_keeps_the_hub(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.add("codex")
    folder = lifecycle.account("codex")
    state_file = folder / "fake-cli.json"
    state = read_json(state_file)
    wiki = "wiki@llm-wiki"
    state["marketplaces"]["llm-wiki"] = str(lifecycle.source)
    state["plugins"].append(wiki)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    root = plugin_root("codex", folder, wiki)
    root.mkdir(parents=True)
    (root / "plugin.json").write_text('{"name":"wiki","version":"1.0.0"}', encoding="utf-8")
    settings = read_json(lifecycle.settings)
    settings["knowledgeEnabled"] = True
    lifecycle.settings.write_text(json.dumps(settings), encoding="utf-8")
    hub = lifecycle.home / ".legion/knowledge"
    hub.mkdir()
    output = lifecycle.invoke_in_child("uninstall", "--yes", answer="")
    ordered(output, f"plugin remove {PLUGIN}", "marketplace remove ps-legion-marketplace", f"plugin remove {wiki}", "marketplace remove llm-wiki", "Results:")
    lifecycle.await_removal()
    assert read_json(state_file) == {"marketplaces": {}, "plugins": []}
    assert hub.is_dir()


def test_uninstall_with_invalid_settings_uses_outlanders(lifecycle: Lifecycle) -> None:
    lifecycle.enable("codex")
    lifecycle.install()
    lifecycle.settings.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.settings.write_text("{broken", encoding="utf-8")
    output = lifecycle.invoke_in_child("uninstall", "--yes", answer="")
    ordered(output, "skip: account folders (settings invalid:", "Uninstall plan:", f"plugin remove {PLUGIN}", "Results:")
    lifecycle.await_removal()
    assert lifecycle.settings.read_text(encoding="utf-8") == "{broken"


def test_purge_keeps_legion_files_after_uv_failure(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    lifecycle.add("claude")
    monkeypatch.setenv("FAKE_UV_FAIL", "uninstall")
    windows = sys.platform == "win32"
    output = lifecycle.invoke_in_child("uninstall", "--purge", answer="y\n", code=0 if windows else 1)
    ordered(output, "Purge plan:", QUESTION, WAITING, f"claude plugin uninstall {PLUGIN}", "Results:", PURGE if windows else "Purge failed.")
    if windows:
        log = lifecycle.home / ".legion/temp/uv-tool.log"
        stop = lifecycle.home / ".legion/run/stop"
        deadline = time.monotonic() + 15
        while not log.is_file() or "uv tool command exited 1" not in log.read_text(encoding="utf-8") or stop.exists():
            assert time.monotonic() < deadline, "Failed handover did not finish"
            time.sleep(0.05)
    assert lifecycle.uv.is_file()
    assert lifecycle.settings.is_file()
    monkeypatch.delenv("FAKE_UV_FAIL")
    lifecycle.invoke_in_child("uninstall", "--purge", "--yes", answer="")
    lifecycle.await_removal(lifecycle.home / ".legion")


@pytest.mark.parametrize(
    ("verb", "flag"),
    [("install", flag) for flag in ("--tool", "--config-dir", "--update", "--uninstall", "--purge")] + [("uninstall", flag) for flag in ("--tool", "--config-dir")],
)
def test_lifecycle_rejects_removed_options(lifecycle: Lifecycle, verb: str, flag: str) -> None:
    assert f"No such option '{flag}'" in lifecycle.invoke(verb, flag, code=2).output


if __name__ == "__main__":
    result = CliRunner().invoke(main, json.loads(sys.argv[1]), input=sys.argv[2])
    print(json.dumps([result.exit_code, result.output]))


@pytest.mark.parametrize("verb", ["install", "update"])
def test_playground_applies_same_version_checkout(lifecycle: Lifecycle, verb: str) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    checkout = lifecycle.source.with_name("second checkout")
    shutil.copytree(lifecycle.source, checkout)
    (checkout / "plugins/legion/helpers.json").write_text("new code", encoding="utf-8")
    output = lifecycle.invoke(verb, "--source", str(checkout), answer="y\n").output
    ordered(output, str(checkout), QUESTION + "\n", "Results:", "installed")
    folder = lifecycle.outlander("claude")
    assert read_json(folder / "fake-cli.json")["marketplaces"]["ps-legion-marketplace"] == str(checkout)
    assert Path(read_json(lifecycle.uv)["source"]) == checkout / "plugins/legion"


def test_playground_mixed_install_reports_already_done(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    lifecycle.enable("codex")
    output = lifecycle.install().output.split("Results:", 1)[1]
    assert next(line for line in output.splitlines() if line.startswith("claude ")).endswith("already done")
    assert "installed legion" in next(line for line in output.splitlines() if line.startswith("codex "))


@pytest.mark.parametrize("verb", ["install", "update"])
def test_playground_repeat_needs_no_approval(lifecycle: Lifecycle, verb: str) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    result = lifecycle.invoke(verb, "--source", str(lifecycle.source), answer="")
    assert QUESTION not in result.output
    assert result.output.splitlines()[-1].startswith("Nothing to do")
    assert "Results:" not in result.output


@pytest.mark.parametrize("reachable", [True, False])
def test_playground_path_result(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, reachable: bool) -> None:
    monkeypatch.setenv("FAKE_UV_FAIL", "update-shell")
    if reachable:
        write_shim(lifecycle.bin_dir, "legion", Path(__file__))
    else:
        monkeypatch.setenv("UV_TOOL_BIN_DIR", str(lifecycle.home / "tool bin"))
    result = lifecycle.install()
    assert "update-shell exited" not in result.output
    last = result.output.splitlines()[-1]
    if reachable:
        assert last.endswith("installed")
    else:
        ordered(last, "not on PATH in this terminal.", "Open a new terminal.", f"If legion is still unavailable, add {lifecycle.home / 'tool bin'} to your PATH.")


def test_playground_purge_result(lifecycle: Lifecycle) -> None:
    lifecycle.install()
    output = lifecycle.invoke_in_child("uninstall", "--purge", answer="y\n")
    if sys.platform == "win32":
        assert "scheduled" in output
    else:
        assert "scheduled" not in output
        assert "legion command removed" in output
    assert "uv-tool.log" not in output
    lifecycle.await_removal(lifecycle.home / ".legion")


def test_playground_uninstall_preview_and_prompt(lifecycle: Lifecycle) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    output = lifecycle.invoke("uninstall", "--dry-run").output
    ordered(output, "Uninstall plan:", "plugin uninstall", "Nothing changed (dry run).\n")
    applied = lifecycle.invoke_in_child("uninstall", answer="y\n")
    ordered(applied, QUESTION + "\n", WAITING + "\n")
    lifecycle.await_removal()


@pytest.mark.parametrize("answer", ["", "n\n"])
def test_playground_add_declines_without_editing(lifecycle: Lifecycle, answer: str) -> None:
    lifecycle.enable("claude")
    output = lifecycle.invoke("add", "claude", "--source", str(lifecycle.source), answer=answer).output
    ordered(output, "Setup plan:", str(lifecycle.source), QUESTION + "\n", "Nothing changed.")
    assert output.count(QUESTION) == 1
    assert not lifecycle.settings.exists()


def test_playground_status_after_install_names_add(lifecycle: Lifecycle) -> None:
    lifecycle.install()
    ordered(lifecycle.invoke("status").output, "no legion settings yet", "legion add <tool>")


def test_playground_catalog_accepts_legacy_installer(lifecycle: Lifecycle) -> None:
    # The 0.23.1 installer rejects unknown plugin fields in a checkout catalog.
    catalog = read_json(lifecycle.source / "plugins/legion/tools.json")
    for tool in catalog["tools"]:
        assert "adoptRewrite" not in tool["plugin"]
    lifecycle.install()


def test_playground_intents_use_checkout_and_current_update_command() -> None:
    intents = REPO / "tests/playground/intents"
    assert "legion install --update" not in (intents / "install-update.md").read_text(encoding="utf-8")
    assert "install.sh --source /work/legion" in (intents / "bootstrap-one-liner.md").read_text(encoding="utf-8")


def test_self_update_operator_instructions() -> None:
    skill = (REPO / "plugins/legion/skills/legion-update/SKILL.md").read_text(encoding="utf-8")
    ordered(skill.split("\n3. ", 1)[1].split("\n4. ", 1)[0], "running members", "handoff", "ask whether")
    reference = (REPO / "docs/reference.md").read_text(encoding="utf-8")
    assert "The original shell stays available on macOS and Linux." in reference


@pytest.fixture
def self_update_repo(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> Path:
    git = TEST_GIT
    assert git
    monkeypatch.setenv("PATH", os.pathsep.join((str(lifecycle.bin_dir), str(Path(git).parent))))
    repo = lifecycle.source.parent / "work repo"
    repo.mkdir()
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    project = repo / ".legion/configuration/project.json"
    project.parent.mkdir(parents=True)
    project.write_text('{"schemaVersion":1}', encoding="utf-8")
    handoff = repo / ".legion/state/handoff.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("Continue the fake task.\n", encoding="utf-8")
    return repo


def test_self_update_requires_an_overseer_lease(lifecycle: Lifecycle, self_update_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(self_update_repo)
    output = lifecycle.invoke("update", "--self", "--yes", "--source", str(lifecycle.source), code=1).output
    assert "lease" in output
    assert not (lifecycle.home / ".legion/run/stop").exists()


@pytest.mark.parametrize(("tool_id", "failure"), [("claude", None), ("claude", "uv"), ("claude", "target"), ("claude", "unexpected"), ("codex", None), ("copilot", None)])
def test_self_update_stops_and_resumes_the_saved_launch(lifecycle: Lifecycle, self_update_repo: Path, monkeypatch: pytest.MonkeyPatch, tool_id: str, failure: str | None) -> None:
    lifecycle.enable(tool_id)
    lifecycle.install()
    lifecycle.add(tool_id)
    repo = self_update_repo
    settings = read_json(lifecycle.settings)
    if tool_id == "claude":
        settings["channels"] = [{"id": "main", "kind": "telegram", "target": "1", "accounts": ["claude1"]}]
    lifecycle.settings.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("FAKE_OVERSEER_REQUEST", str(lifecycle.source))
    tool = Path(os.environ["UV_TOOL_DIR"]) / "legion"
    tool.mkdir()
    original = tool / "installed"
    original.write_text("old installed environment", encoding="utf-8")
    entry = lifecycle.bin_dir / ("legion.cmd" if sys.platform == "win32" else "legion")
    old_entry = entry.read_bytes()
    if failure == "uv":
        monkeypatch.setenv("FAKE_UV_FAIL", "install")
    if failure == "target":
        monkeypatch.setenv("FAKE_CLI_FAIL", "plugin uninstall")
    if failure == "unexpected":
        # Inject a process-start failure after the stop event through Python's public audit boundary.
        injection = repo / "injection"
        injection.mkdir()
        socket_fence = (REPO / "tests/python/socket_fence/sitecustomize.py").read_text(encoding="utf-8")
        (injection / "sitecustomize.py").write_text(
            socket_fence
            + """

import sys

def fail_after_stop(event, args):
    if event == "subprocess.Popen" and Path(os.environ["LEGION_HOME"], ".legion/run/stop").is_file():
        raise KeyError("injected process-start failure")

if "LEGION_HOME" in os.environ:
    sys.addaudithook(fail_after_stop)
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (str(injection), os.environ.get("PYTHONPATH")))))
    lifecycle.publish_update()
    output = repo / "launcher.log"
    with output.open("wb") as stream:
        launcher = subprocess.Popen(
            [sys.executable, "-m", "legion.cli", "--home", str(lifecycle.home), "--argument=--model", "--argument=fake model"],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
        )
    try:
        if tool_id == "codex":
            ready = repo / "codex-ready"
            deadline = time.monotonic() + 30
            while not ready.exists() and launcher.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), output.read_text(encoding="utf-8")
            request = subprocess.run(
                [sys.executable, "-m", "legion.cli", "--home", str(lifecycle.home), "update", "--self", "--yes", "--source", str(lifecycle.source)],
                cwd=repo,
                env=os.environ | {"CODEX_THREAD_ID": FAKE_SESSION},
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert request.returncode == 0, request.stderr
        assert launcher.wait(timeout=40) == 0, output.read_text(encoding="utf-8")
        launched_output = output.read_text(encoding="utf-8")
        assert "stopped for an update" in launched_output
        assert "the launch failed" not in launched_output
        starts = repo / "overseer.jsonl"
        result = repo / ".legion/temp/self-update.log"
        deadline = time.monotonic() + 40
        resumed_config = repo / "resumed-config.json"
        while not resumed_config.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert starts.exists(), output.read_text(encoding="utf-8")
        assert len(calls(starts)) == 2, output.read_text(encoding="utf-8")
        first, resumed = calls(starts)
        extra = ["--model", "fake model"]
        channel = None
        if tool_id == "claude":
            assert first[0] == "--session-id"
            extra.extend(["--channels", "plugin:telegram@claude-plugins-official", "--"])
            assert resumed[:-1] == ["--resume", first[1], *extra]
            channel = str(lifecycle.home / ".legion/channels/telegram/main")
        elif tool_id == "codex":
            assert resumed[:-1] == ["resume", FAKE_SESSION, *extra]
        else:
            assert resumed[:-1] == [f"--resume={FAKE_SESSION}", *extra, "-i"]
        assert "handoff" in resumed[-1]
        assert json.loads(resumed_config.read_text(encoding="utf-8")) == [str(lifecycle.account(tool_id)), channel]
        assert result.read_text(encoding="utf-8").startswith("Legion self-update failed" if failure else "Legion self-update completed")
        if failure == "unexpected":
            assert "KeyError" in (lifecycle.home / ".legion/temp/self-update.err").read_text(encoding="utf-8")
        assert read_json(lifecycle.uv)["version"] == (lifecycle.version if failure else UPDATED)
        if failure:
            assert entry.read_bytes() == old_entry
            assert original.read_text(encoding="utf-8") == "old installed environment"
        if failure == "target":
            assert sum(line[:2] == ["tool", "install"] for line in calls(lifecycle.uv.with_suffix(".log"))) == 1
        if failure == "uv":
            assert json.loads(lifecycle.uv.with_suffix(".recovery").read_text(encoding="utf-8")) == [str(lifecycle.home / ".legion/temp")]
            assert not list((lifecycle.home / ".legion/temp").glob("legion-recovery-*"))
        assert (repo / ".legion/state/handoff.md").read_text(encoding="utf-8") == "Continue the fake task.\n"
        assert not (lifecycle.home / ".legion/run/stop").exists()
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait()


@pytest.mark.parametrize("worker_import_fails", [False, True])
def test_bare_installed_self_update_uses_its_own_runtime(lifecycle: Lifecycle, self_update_repo: Path, monkeypatch: pytest.MonkeyPatch, worker_import_fails: bool) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    lifecycle.add("claude")
    lifecycle.publish_update()
    tool = Path(os.environ["UV_TOOL_DIR"]) / "legion"
    shutil.copytree(sys.prefix, tool, symlinks=sys.platform != "win32")
    python = tool / Path(sys.executable).relative_to(sys.prefix)
    monkeypatch.setenv("FAKE_OVERSEER_REQUEST", str(lifecycle.source))
    monkeypatch.setenv("FAKE_OVERSEER_BARE_PYTHON", str(python))
    if worker_import_fails:
        injection = self_update_repo / "injection"
        injection.mkdir()
        (injection / "sitecustomize.py").write_text(
            (REPO / "tests/python/socket_fence/sitecustomize.py").read_text(encoding="utf-8")
            + "\nimport sys\n"
            + "if sys.argv[0] == '-m' and any('legion-worker-' in part for part in Path(sys.prefix).parts):\n"
            + "    sys.modules['legion.services.lifecycle._self_update'] = None\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (str(injection), os.environ.get("PYTHONPATH")))))
    repo = self_update_repo
    output = repo / "launcher.log"
    with output.open("wb") as stream:
        launcher = subprocess.Popen(
            [sys.executable, "-m", "legion.cli", "--home", str(lifecycle.home)],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
        )
    try:
        result = repo / ".legion/temp/self-update.log"
        last_line = "Legion self-update failed before stopping the overseer." if worker_import_fails else "Legion self-update completed."
        deadline = time.monotonic() + 45
        request = repo / "self-update-request.txt"
        while not (result.exists() and result.read_text(encoding="utf-8").startswith(last_line)):
            if request.exists():
                request_output = request.read_text(encoding="utf-8")
                assert result.exists() or "self-update queued" in request_output, request_output
            assert launcher.poll() in (None, 0), output.read_text(encoding="utf-8")
            assert time.monotonic() < deadline, output.read_text(encoding="utf-8")
            time.sleep(0.05)
        assert not any(args[:2] == ["tool", "run"] for args in calls(lifecycle.uv.with_suffix(".log")))
        if worker_import_fails:
            assert launcher.poll() is None
            assert len(calls(repo / "overseer.jsonl")) == 1
            assert not (lifecycle.home / ".legion/run/stop").exists()
            assert read_json(lifecycle.uv)["version"] == lifecycle.version
        else:
            assert launcher.wait(timeout=10) == 0
            resumed = repo / "resumed-config.json"
            while not resumed.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert resumed.exists()
            assert read_json(lifecycle.uv)["version"] == UPDATED
            assert not tool.exists()
            first, second = calls(repo / "overseer.jsonl")
            assert second[:2] == ["--resume", first[1]]
    finally:
        if launcher.poll() is None:
            children = psutil.Process(launcher.pid).children(recursive=True)
            for child in reversed(children):
                with suppress(psutil.NoSuchProcess):
                    child.kill()
            psutil.wait_procs(children, timeout=5)
            launcher.kill()
            launcher.wait()


@pytest.mark.parametrize("other_session", [False, True])
def test_self_update_keeps_live_sessions_when_no_restart_is_safe(lifecycle: Lifecycle, self_update_repo: Path, monkeypatch: pytest.MonkeyPatch, other_session: bool) -> None:
    lifecycle.enable("claude")
    lifecycle.install()
    lifecycle.add("claude")
    monkeypatch.setenv("FAKE_OVERSEER_REQUEST", str(lifecycle.source))
    run = lifecycle.home / ".legion/run"
    run.mkdir(exist_ok=True)
    if other_session:
        lifecycle.publish_update()
    child_code = "import time; time.sleep(60)"
    with subprocess.Popen([sys.executable, "-c", child_code]) as member:
        registration = run / f"{member.pid}.pid"
        if other_session:
            registration.write_text(str(member.pid), encoding="ascii")
        with subprocess.Popen([sys.executable, "-m", "legion.cli", "--home", str(lifecycle.home)], cwd=self_update_repo, stdin=subprocess.DEVNULL) as launcher:
            try:
                status = self_update_repo / "self-update-request.txt"
                deadline = time.monotonic() + 15
                while not status.exists() and launcher.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                expected = f"other Legion sessions are running (process {member.pid})" if other_session else "skip: self-update (already current)"
                assert expected in status.read_text(encoding="utf-8")
                assert member.poll() is None
                if not other_session:
                    assert launcher.poll() is None
                assert not (self_update_repo / ".legion/temp/self-update.log").exists()
                assert not (run / "stop").exists()
                assert len(calls(self_update_repo / "overseer.jsonl")) == 1
            finally:
                registration.unlink(missing_ok=True)
                member.kill()
                # Let Legion and the fake session leave normally in the no-op case.
                if launcher.poll() is None:
                    children = psutil.Process(launcher.pid).children(recursive=True)
                    for child in reversed(children):
                        with suppress(psutil.NoSuchProcess):
                            child.kill()
                    psutil.wait_procs(children, timeout=5)
                    launcher.kill()
