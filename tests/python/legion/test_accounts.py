import json
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from cli_runner import run
from filelock import FileLock
from launcher_fakes import (
    WINDOWS,
    fake_cli,
    login,
    only_on_path,
    tool,
)

from legion.services.accounts import _accounts
from legion.core import _settings_io
from legion.core._home import settings_path

OLD_FOLDER = "Existing account config folder does not exist."


class World:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        self.home.mkdir()
        self.tools = base / "tools"
        self.custom = fake_cli(base / "custom", "my-codex")

    def agents(self) -> list[dict[str, Any]]:
        return json.loads(settings_path(self.home).read_text(encoding="utf-8"))["agents"]

    def channels(self) -> list[dict[str, Any]]:
        return json.loads(settings_path(self.home).read_text(encoding="utf-8"))["channels"]

    def folder(self, account: str) -> Path:
        return self.home / ".legion" / "agents" / account

    def settings_bytes(self) -> bytes:
        return settings_path(self.home).read_bytes()


def write_settings(home: Path, data: dict[str, Any]) -> None:
    path = settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_path)
    for name, variable in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("copilot", "COPILOT_HOME"), ("taken", "TAKEN")):
        fake_cli(world.tools, name, variable)
    only_on_path(monkeypatch, world.tools)
    monkeypatch.chdir(tmp_path)
    return world


def legion(world: World, *words: str) -> int:
    return run(["--home", str(world.home), *words])


# `remove` is a top-level verb; the other account verbs live under `account`.
def account(world: World, *words: str) -> int:
    return legion(world, *words) if words[0] == "remove" else legion(world, "account", *words)


def add(world: World, account_id: str = "codex-a", tool_id: str = "codex", *more: str) -> int:
    return account(world, "add", "--id", account_id, "--tool", tool_id, *more)


def says(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_account_add_writes_the_settings_and_the_folder(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world) == 0
    assert says(capsys) == (f"Log in once: {login('CODEX_HOME', world.folder('codex-a'), 'codex')}\n", "")
    assert world.agents() == [{"id": "codex-a", "tool": "codex", "configDir": str(world.folder("codex-a"))}]
    assert world.folder("codex-a").is_dir()
    before = world.settings_bytes()
    settings = json.loads(before)
    assert settings["schemaVersion"] == 1
    assert settings["channels"] == []
    assert add(world) == 0
    assert says(capsys) == ("skip: account codex-a (already done)\n", "")
    assert world.settings_bytes() == before


def test_new_account_without_the_stock_cli_is_refused(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    only_on_path(monkeypatch, world.base / "empty")
    assert add(world) == 1
    assert says(capsys) == ("", "codex is not installed; run legion tool install --tool codex\n")
    assert not settings_path(world.home).exists()


def test_new_account_stores_a_command_that_is_not_the_stock_cli(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a", "codex", "--command", str(world.custom)) == 0
    assert world.agents()[0]["command"] == str(world.custom)
    assert add(world, "codex-b", "codex", "--command", str(world.tools / ("codex.cmd" if WINDOWS else "codex"))) == 0
    assert "command" not in world.agents()[1]
    capsys.readouterr()


def test_add_with_a_command_sets_the_command(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    folder = world.base / "existing"
    folder.mkdir()
    assert account(world, "add", "--id", "codex-a", "--tool", "codex", "--command", str(world.custom), "--config-dir", str(folder)) == 0
    assert says(capsys)[0].splitlines()[-1] == f"Copied {folder} into {world.folder('codex-a')}; open it with 'legion --account codex-a'."
    assert world.agents() == [{"id": "codex-a", "tool": "codex", "configDir": str(world.folder("codex-a")), "command": str(world.custom)}]
    # On an existing account it is the repair path: the stored command is replaced by the registered one.
    other = fake_cli(world.base / "other", "other-codex")
    assert account(world, "add", "--id", "codex-a", "--tool", "codex", "--command", str(other)) == 0
    assert says(capsys)[0].splitlines() == ["wrote: command of account codex-a"]
    assert world.agents()[0]["command"] == str(other)


def test_add_with_the_stock_command_clears_the_stored_command(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    folder = world.base / "existing"
    folder.mkdir()
    assert account(world, "add", "--id", "codex-a", "--tool", "codex", "--command", str(world.custom), "--config-dir", str(folder)) == 0
    stock = world.tools / ("codex.cmd" if WINDOWS else "codex")
    assert account(world, "add", "--id", "codex-a", "--tool", "codex", "--command", str(stock)) == 0
    assert "command" not in world.agents()[0]
    capsys.readouterr()


def test_adopting_an_existing_folder_copies_it_into_the_account(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    folder = world.base / "existing account"
    folder.mkdir()
    (folder / "settings.json").write_text('{"theme":"dark"}', encoding="utf-8")
    assert add(world, "claude-existing", "claude", "--config-dir", str(folder)) == 0
    target = world.folder("claude-existing")
    assert says(capsys)[0].splitlines() == [f"copying {folder} into {target}", f"Copied {folder} into {target}; open it with 'legion --account claude-existing'."]
    assert world.agents() == [{"id": "claude-existing", "tool": "claude", "configDir": str(target)}]
    assert (target / "settings.json").read_text(encoding="utf-8") == '{"theme":"dark"}'
    assert (folder / "settings.json").read_text(encoding="utf-8") == '{"theme":"dark"}'
    before = world.settings_bytes()
    assert add(world, "claude-existing", "claude", "--config-dir", str(target)) == 0
    assert says(capsys) == ("skip: account claude-existing (already done)\n", "")
    assert world.settings_bytes() == before


def test_a_relative_or_home_config_dir_is_copied(world: World) -> None:
    (world.base / "existing account").mkdir()
    (world.home / "from-home").mkdir()
    (world.home / "from-home" / "marker.txt").write_text("home", encoding="utf-8")
    assert add(world, "codex-a", "codex", "--config-dir", "./existing account") == 0
    assert add(world, "codex-b", "codex", "--config-dir", "~/from-home") == 0
    assert [entry["configDir"] for entry in world.agents()] == [str(world.folder("codex-a")), str(world.folder("codex-b"))]
    assert (world.folder("codex-b") / "marker.txt").read_text(encoding="utf-8") == "home"


def test_a_missing_config_dir_and_a_different_folder_for_an_existing_id_are_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "claude-a", "claude", "--config-dir", "~/missing") == 1
    assert says(capsys) == ("", OLD_FOLDER + "\n")
    assert not settings_path(world.home).exists()
    assert add(world, "claude-a", "claude") == 0
    before = world.settings_bytes()
    capsys.readouterr()
    assert add(world, "claude-a", "claude", "--config-dir", str(world.base)) == 1
    assert says(capsys) == ("", "account 'claude-a' already has its folder; legion add claude <folder> copies a folder into a new account\n")
    assert world.settings_bytes() == before


def test_a_target_with_files_is_refused_and_the_source_stays(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    folder = world.base / "existing"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    world.folder("codex-a").mkdir(parents=True)
    (world.folder("codex-a") / "other.txt").write_text("x", encoding="utf-8")
    assert add(world, "codex-a", "codex", "--config-dir", str(folder)) == 1
    assert "already has files" in says(capsys)[1]
    assert not settings_path(world.home).exists()
    assert (folder / "a.txt").read_text(encoding="utf-8") == "a"


@pytest.mark.parametrize(("account_id", "tool_id", "message"), [("Bad id", "codex", "id 'Bad id' must match ^[a-z][a-z0-9-]*$"), ("codex-a", "unknown-tool", "unknown tool 'unknown-tool'")])
def test_an_invalid_first_add_creates_no_settings_folder(world: World, capsys: pytest.CaptureFixture[str], account_id: str, tool_id: str, message: str) -> None:
    assert account(world, "add", "--id", account_id, "--tool", tool_id) == 1
    assert says(capsys) == ("", message + "\n")
    assert not (world.home / ".legion").exists()


@pytest.mark.parametrize("words", [["rename", "--id", "codex-a", "--new-id", "codex-b"], ["remove", "--id", "codex-a"]])
def test_the_other_verbs_need_settings(world: World, capsys: pytest.CaptureFixture[str], words: list[str]) -> None:
    assert account(world, *words) == 1
    assert says(capsys) == ("", "No legion settings yet; add an agent first.\n")
    assert list(world.home.iterdir()) == []


@pytest.mark.parametrize(
    ("account_id", "message"),
    [("legion", "'legion' is reserved for an owner command"), ("Invalid_id", "id 'Invalid_id' must match ^[a-z][a-z0-9-]*$"), ("taken", "'taken' is already a command")],
)
def test_ids_that_cannot_name_an_account_are_refused(world: World, capsys: pytest.CaptureFixture[str], account_id: str, message: str) -> None:
    assert add(world, "claude-a", "claude") == 0
    before = world.settings_bytes()
    capsys.readouterr()
    assert add(world, account_id, "codex") == 1
    assert says(capsys) == ("", message + "\n")
    assert world.settings_bytes() == before


def test_a_missing_command_file_is_refused_in_one_line(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a", "codex", "--command", str(world.base / "gone")) == 1
    assert says(capsys)[1].count("\n") == 1
    assert not settings_path(world.home).exists()


@pytest.mark.parametrize("custom", [False, True])
def test_repeat_recreates_a_missing_managed_config_folder(world: World, capsys: pytest.CaptureFixture[str], custom: bool) -> None:
    more = ["--command", str(world.custom)] if custom else []
    assert add(world, "codex-a", "codex", *more) == 0
    before = world.settings_bytes()
    world.folder("codex-a").rmdir()
    capsys.readouterr()
    assert add(world) == 0
    assert world.folder("codex-a").is_dir()
    assert says(capsys) == (f"created: config folder {world.folder('codex-a')}\n", "")
    assert world.settings_bytes() == before


def test_repeat_recovers_after_config_folder_creation_failed(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mkdir = Path.mkdir

    def fail_config(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == world.folder("codex-a"):
            raise OSError("fixture config creation failed")
        mkdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", fail_config)
        assert add(world) == 1
    assert says(capsys)[1] == "fixture config creation failed\n"
    assert add(world) == 0
    assert world.folder("codex-a").is_dir()
    assert "already done" not in says(capsys)[0]


@pytest.mark.parametrize("interpreted", [False, True])
def test_add_requires_executable_native_posix_target(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], interpreted: bool) -> None:
    command = world.base / ("custom.ps1" if interpreted else "custom-script")
    command.write_text("exit 3\n", encoding="utf-8")
    command.chmod(0o644)
    # Exercise the POSIX decision here too; actual mode enforcement runs on Linux and macOS.
    monkeypatch.setattr(_accounts, "POSIX", True)
    if WINDOWS:
        real_access = _accounts.os.access
        monkeypatch.setattr(_accounts.os, "access", lambda path, mode: False if Path(path) == command and mode == _accounts.os.X_OK else real_access(path, mode))
    words = ("add", "--id", "codex-a", "--tool", "codex", "--command", str(command))
    if interpreted:
        assert account(world, *words) == 0
    else:
        assert account(world, *words) == 1
        assert "not executable" in says(capsys)[1]
        assert not settings_path(world.home).exists()


@pytest.mark.parametrize("verb", ["rename", "remove"])
def test_failed_account_change_can_be_retried(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verb: str) -> None:
    assert add(world) == 0
    assert add(world, "codex-b") == 0
    bind(world, "codex-a", "codex-b")
    before = world.settings_bytes()
    words = [verb, "--id", "codex-a", *(["--new-id", "codex-new"] if verb == "rename" else [])]
    capsys.readouterr()

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("fixture operation failed")

    with monkeypatch.context() as patch:
        patch.setattr(_settings_io, "save_locked", fail)
        assert account(world, *words) == 1
    assert "fixture operation failed" in says(capsys)[1]
    assert world.settings_bytes() == before
    assert account(world, *words) == 0
    assert says(capsys)[1] == ""
    assert world.channels()[0]["accounts"] == (["codex-new", "codex-b"] if verb == "rename" else ["codex-b"])


def test_remove_validates_remaining_settings(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world) == 0
    bind(world, "codex-a")
    data = json.loads(world.settings_bytes())
    data["channels"][0]["accounts"].append("unknown-account")
    write_settings(world.home, data)
    before = world.settings_bytes()
    capsys.readouterr()
    assert account(world, "remove", "--id", "codex-a") == 1
    assert "unknown-account" in says(capsys)[1]
    assert world.settings_bytes() == before


def test_an_account_whose_command_file_is_gone_needs_the_owner(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a", "codex", "--command", str(world.custom)) == 0
    world.custom.unlink()
    capsys.readouterr()
    assert add(world) == 1
    assert says(capsys) == ("", "target needed: codex-a; ask the owner for the registered executable\n")
    # register names the executable once, and the account remembers it.
    other = fake_cli(world.base / "other", "other-codex")
    assert account(world, "add", "--id", "codex-a", "--tool", "codex", "--command", str(other), "--config-dir", str(world.folder("codex-a"))) == 0
    assert world.agents()[0]["command"] == str(other)


def bind(world: World, *accounts: str) -> None:
    arguments = ["--home", str(world.home), "channel", "add", "--id", "main", "--kind", "telegram", "--target", "123"]
    for name in accounts:
        arguments += ["--account", name]
    assert run(arguments) == 0


def test_rename_moves_bindings(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a") == 0
    assert add(world, "codex-b") == 0
    bind(world, "codex-a", "codex-b")
    capsys.readouterr()
    assert account(world, "rename", "--id", "codex-a", "--new-id", "codex-new") == 0
    assert says(capsys) == (f"Config folder kept at {world.folder('codex-a')}; rename it yourself only if you want to.\n", "")
    assert [entry["id"] for entry in world.agents()] == ["codex-new", "codex-b"]
    assert world.channels()[0]["accounts"] == ["codex-new", "codex-b"]


def test_a_repeated_rename_skips(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world) == 0
    assert account(world, "rename", "--id", "codex-a", "--new-id", "codex-new") == 0
    before = world.settings_bytes()
    capsys.readouterr()
    assert account(world, "rename", "--id", "codex-a", "--new-id", "codex-new") == 0
    assert says(capsys) == ("skip: account rename codex-a to codex-new (already done)\n", "")
    assert world.settings_bytes() == before


def test_rename_refusals_change_nothing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a") == 0
    assert add(world, "codex-b") == 0
    before = world.settings_bytes()
    capsys.readouterr()
    for old, new, message in (
        ("codex-a", "codex-b", "account 'codex-b' already exists"),
        ("codex-a", "taken", "'taken' is already a command"),
        ("codex-a", "Bad_id", "id 'Bad_id' must match ^[a-z][a-z0-9-]*$"),
        ("codex-a", "legion", "'legion' is reserved for an owner command"),
        ("nobody", "codex-d", "unknown account 'nobody'"),
    ):
        assert account(world, "rename", "--id", old, "--new-id", new) == 1
        assert says(capsys) == ("", message + "\n")
    assert world.settings_bytes() == before


def test_remove_refuses_the_last_account_of_a_channel(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a") == 0
    bind(world, "codex-a")
    before = world.settings_bytes()
    capsys.readouterr()
    assert account(world, "remove", "--id", "codex-a") == 1
    assert says(capsys) == ("", "channel 'main' would have no accounts; rebind or remove it first\n")
    assert world.settings_bytes() == before


def test_remove_prunes_the_binding_and_keeps_the_folder(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a") == 0
    assert add(world, "codex-b") == 0
    bind(world, "codex-a", "codex-b")
    capsys.readouterr()
    assert account(world, "remove", "--id", "codex-a") == 0
    assert says(capsys) == (f"Config folder kept at {world.folder('codex-a')}.\n", "")
    assert [entry["id"] for entry in world.agents()] == ["codex-b"]
    assert world.channels()[0]["accounts"] == ["codex-b"]
    assert world.folder("codex-a").is_dir()
    assert account(world, "remove", "--id", "codex-a") == 0
    assert says(capsys) == ("skip: account remove codex-a (already done)\n", "")


def test_a_positional_id_is_a_usage_error(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world) == 0
    before = world.settings_bytes()
    capsys.readouterr()
    for words in (["remove", "codex-a"], ["account", "rename", "codex-a", "codex-b"], ["account", "add", "codex-a", "--tool", "codex"]):
        assert legion(world, *words) == 2
    assert world.settings_bytes() == before
    assert legion(world, "--help") == 0
    assert "Remove an account (its folder is kept)" in says(capsys)[0]


@contextmanager
def watched_lock() -> Iterator[list[str]]:
    events: list[str] = []
    real_lock, real_read, real_save = _settings_io.FileLock, _settings_io._validate.read_text, _settings_io.save_locked
    held = False

    @contextmanager
    def lock(path: str, timeout: int) -> Iterator[None]:
        nonlocal held
        with real_lock(path, timeout=timeout):
            held = True
            try:
                yield
            finally:
                held = False

    def read(path: Path) -> str:
        events.append("read under lock" if held else "read without lock")
        return real_read(path)

    def save(home: Path, settings: Any) -> None:
        events.append("save under lock" if held else "save without lock")
        real_save(home, settings)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_settings_io, "FileLock", lock)
        patch.setattr(_settings_io._validate, "read_text", read)
        patch.setattr(_settings_io, "save_locked", save)
        yield events


@pytest.mark.parametrize("verb", ["add", "rename", "remove"])
def test_every_change_reads_and_writes_the_settings_under_the_writer_lock(world: World, verb: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert add(world, "codex-a") == 0
    words = {"add": ["add", "--id", "codex-b", "--tool", "codex"], "rename": ["rename", "--id", "codex-a", "--new-id", "codex-b"], "remove": ["remove", "--id", "codex-a"]}[verb]
    with watched_lock() as events:
        assert account(world, *words) == 0
    assert events
    assert all(event.endswith("under lock") for event in events)
    assert "save under lock" in events
    capsys.readouterr()


def test_a_busy_settings_lock_is_one_line_and_changes_nothing(world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert add(world, "codex-a") == 0
    before = world.settings_bytes()
    real_lock = _settings_io.FileLock
    capsys.readouterr()
    with FileLock(str(settings_path(world.home)) + ".lock", timeout=0):

        def immediate(path: str, timeout: int) -> FileLock:
            assert timeout == 30
            return real_lock(path, timeout=0)

        monkeypatch.setattr(_settings_io, "FileLock", immediate)
        assert add(world, "codex-b") == 1
    assert says(capsys) == ("", "legion settings are busy; another legion command is still running\n")
    assert world.settings_bytes() == before


# The login command quotes the folder and the account's own program for the system's shell, a quote in a name included.
def test_the_login_command_quotes_for_the_shell(tmp_path: Path) -> None:
    folder = tmp_path / "o'brien a"
    program = tmp_path / "my claude.exe"
    line = _accounts.login_command(tool("claude"), folder, str(program), tmp_path)
    if WINDOWS:
        quoted = str(folder).replace("'", "''")
        assert line == f"$env:CLAUDE_CONFIG_DIR='{quoted}'; & '{program}'"
    else:
        assert line == f"CLAUDE_CONFIG_DIR={shlex.quote(str(folder))} {shlex.quote(str(program))}"


# Without its own program the account starts the tool by its plain name.
def test_the_login_command_names_the_tool_without_a_program(tmp_path: Path) -> None:
    assert _accounts.login_command(tool("codex"), tmp_path / "a", None, tmp_path) == login("CODEX_HOME", tmp_path / "a", "codex")
