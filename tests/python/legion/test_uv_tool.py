# Install, update and uninstall of the legion command through a recording fake uv. No test starts the real uv.
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from app_fakes import FakeUv, make_link, make_source

from legion.core import _settings_io, _stop_event
from legion.core._home import settings_path
from legion.services.installation import _user_path, _uv_tool


@pytest.fixture
def uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeUv:
    return FakeUv(monkeypatch, tmp_path / "uv bin")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_install_exports_the_lock_then_installs_non_editable_with_the_extra(tmp_path: Path, home: Path, uv: FakeUv) -> None:
    source = make_source(tmp_path)
    assert _uv_tool.install(source, home)[0].startswith("legion command 1.0.0")
    export = next(call for call in uv.calls if call[0] == "export")
    assert export == ["export", "--frozen", "--no-default-groups", "--extra", "mcp", "--no-emit-project", "--no-hashes"]
    (install,) = uv.installs()
    assert install[:3] == ["tool", "install", f"{source}[mcp]"]
    assert install[3] == "--constraints"
    assert install[5] == "--reinstall"
    assert not Path(install[4]).parent.exists()


def test_the_constraints_file_holds_the_exported_lock(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = uv.run_foreground

    def run(command: str, arguments: list[str]) -> int:
        seen.append(Path(arguments[arguments.index("--constraints") + 1]).read_text(encoding="utf-8"))
        return real(command, arguments)

    monkeypatch.setattr(_uv_tool, "run_foreground", run)
    _uv_tool.install(make_source(tmp_path), home)
    assert seen == ["mcp==1.0.0\n"]


def test_update_shell_runs_only_with_the_owners_yes(tmp_path: Path, home: Path, uv: FakeUv) -> None:
    _uv_tool.install(make_source(tmp_path), home)
    assert ["tool", "update-shell"] not in uv.calls
    _uv_tool.install(make_source(tmp_path / "again", "2.0.0"), home, update_shell=True)
    assert ["tool", "update-shell"] in uv.calls


@pytest.mark.skipif(sys.platform != "win32", reason="Windows saved PATH")
def test_a_folder_on_the_windows_process_path_can_still_be_missing_from_the_saved_path(uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(uv.bin))
    monkeypatch.setattr(_user_path, "persistent_folders", list)
    assert _uv_tool.path_missing("uv")
    monkeypatch.setattr(_user_path, "persistent_folders", lambda: [str(uv.bin)])
    assert not _uv_tool.path_missing("uv")


def test_a_dry_run_shows_update_shell_only_when_it_would_run(tmp_path: Path, home: Path, uv: FakeUv) -> None:
    source = make_source(tmp_path)
    without = _uv_tool.install(source, home, dry_run=True)
    assert not any("update-shell" in line for line in without)
    with_shell = _uv_tool.install(source, home, dry_run=True, update_shell=True)
    assert "tool update-shell" in with_shell[-1]
    assert _uv_tool.STOP_NOTICE in with_shell
    assert any("tool install" in line and "--reinstall" in line for line in with_shell)
    assert uv.calls == [["tool", "list"], ["tool", "list"]]
    assert uv.detached == []
    assert not home.exists()


def test_install_refuses_broken_settings_before_uv_runs(tmp_path: Path, uv: FakeUv) -> None:
    path = settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(_settings_io.SettingsError):
        _uv_tool.install(make_source(tmp_path), tmp_path)
    assert uv.calls == []


def test_a_linked_legion_folder_is_refused_before_settings_lock(tmp_path: Path, uv: FakeUv) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_link(tmp_path / "home" / ".legion", elsewhere)
    with pytest.raises(_settings_io.SettingsError, match="link"):
        _uv_tool.install(make_source(tmp_path), tmp_path / "home")
    assert list(elsewhere.iterdir()) == []
    assert uv.calls == []


def test_a_repeat_of_the_installed_version_prints_skip_and_changes_nothing(tmp_path: Path, home: Path, uv: FakeUv) -> None:
    source = make_source(tmp_path)
    _uv_tool.install(source, home)
    uv.calls.clear()
    assert _uv_tool.install(source, home) == ["skip: legion command 1.0.0 (already done)"]
    assert uv.calls == [["tool", "list"]]
    assert not _stop_event.stop_path(home).exists()


def test_a_new_plugin_version_replaces_the_installed_one(tmp_path: Path, home: Path, uv: FakeUv) -> None:
    uv.installed = "0.9.0"
    assert _uv_tool.install(make_source(tmp_path), home)[0].startswith("legion command 1.0.0")
    assert uv.installed == "1.0.0"


def test_an_install_raises_the_stop_event_before_uv_runs_and_lifts_it_after(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    raised_during: list[bool] = []
    real = uv.run_foreground

    def run(command: str, arguments: list[str]) -> int:
        raised_during.append(_stop_event.stop_path(home).is_file())
        return real(command, arguments)

    monkeypatch.setattr(_uv_tool, "run_foreground", run)
    _uv_tool.install(make_source(tmp_path), home)
    assert raised_during == [True]
    assert not _stop_event.stop_path(home).exists()


def test_a_detached_install_starts_the_helper_and_leaves_the_stop_event_to_it(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_uv_tool, "sys", SimpleNamespace(platform="win32", prefix=str(uv.tools / "legion")))
    source = make_source(tmp_path)
    lines = _uv_tool.install(source, home)
    assert lines[0] == f"legion command 1.0.0 replacement scheduled. Check the result in {home / _uv_tool.HANDOVER_LOG}."
    assert uv.logs == [home / ".legion/temp/uv-tool.log"]
    assert uv.installs() == []
    ((command, remove),) = uv.detached
    assert command[:3] == ["uv", "tool", "install"]
    assert command[3] == f"{source}[mcp]"
    constraints, stop = remove
    assert command[command.index("--constraints") + 1] == str(constraints / "constraints.txt")
    assert (constraints / "constraints.txt").read_text(encoding="utf-8") == "mcp==1.0.0\n"
    assert stop == _stop_event.stop_path(home)
    assert stop.is_file()
    shutil.rmtree(constraints)


@pytest.mark.parametrize(("platform", "inside", "expected"), [("win32", True, True), ("win32", False, False), ("linux", True, False), ("darwin", True, False)])
def test_only_windows_self_replacement_needs_a_handover(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch, platform: str, inside: bool, expected: bool) -> None:
    uv.installed = "0.9.0"
    prefix = str(uv.tools / "legion" if inside else tmp_path / "elsewhere")
    monkeypatch.setattr(_uv_tool, "sys", SimpleNamespace(platform=platform, prefix=prefix))
    monkeypatch.setenv("PATH", str(uv.bin))
    monkeypatch.setattr(_user_path, "persistent_folders", list)
    lines = _uv_tool.install(make_source(tmp_path), home, updating=True)
    assert bool(uv.detached) is expected
    if expected:
        assert lines[-1].startswith("legion command 1.0.0 replacement scheduled.")
        assert "restart your sessions" in lines[-1].lower()
    else:
        assert lines[-1] == "legion 1.0.0 is installed; restart your sessions."
    for _command, remove in uv.detached:
        shutil.rmtree(remove[0])


def test_a_running_legion_that_does_not_stop_blocks_the_install_with_a_plain_message(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_stop_event, "stop_all", lambda _home, **_options: [4242, 4343])
    with pytest.raises(_uv_tool.UvToolError, match=r"legion is still running after 30 seconds \(process 4242, 4343\); close those sessions"):
        _uv_tool.install(make_source(tmp_path), home)
    assert uv.installs() == []
    assert not any(call[0] == "export" for call in uv.calls)
    assert not _stop_event.stop_path(home).exists()


def test_a_failed_install_reports_the_exit_code_leaves_no_constraints_file_and_lifts_the_stop_event(tmp_path: Path, home: Path, uv: FakeUv) -> None:
    uv.fail["tool install"] = 2
    with pytest.raises(_uv_tool.UvToolError, match=r"^uv tool install exited 2$"):
        _uv_tool.install(make_source(tmp_path), home)
    assert not (home / ".legion/temp/uv-tool-install.log").exists()
    assert not list(Path(tempfile.gettempdir()).glob("legion-constraints-*"))
    assert not _stop_event.stop_path(home).exists()


def test_a_failed_export_stops_before_installing(tmp_path: Path, home: Path, uv: FakeUv) -> None:
    uv.fail["export --frozen"] = 1
    with pytest.raises(_uv_tool.UvToolError, match="uv export failed"):
        _uv_tool.install(make_source(tmp_path), home)
    assert uv.installs() == []
    assert not _stop_event.stop_path(home).exists()


def test_a_failed_update_shell_reports_the_path_action(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(_user_path, "persistent_folders", list)
    uv.fail["tool update-shell"] = 1
    lines = _uv_tool.install(make_source(tmp_path), home, update_shell=True)
    assert lines == [f"legion command 1.0.0 is installed but not on PATH in this terminal. Open a new terminal. If legion is still unavailable, add {uv.bin} to your PATH."]
    assert uv.installed == "1.0.0"


def test_without_uv_install_says_so_and_runs_nothing(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_uv_tool, "find_uv", lambda: None)
    source = make_source(tmp_path)
    with pytest.raises(_uv_tool.UvToolError, match="uv is missing"):
        _uv_tool.install(source, home)
    assert _uv_tool.install(source, home, dry_run=True) == [_uv_tool.UV_MISSING]
    assert uv.calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows schedules tool removal")
def test_uninstall_hands_only_the_tool_removal_to_the_helper_with_the_stop_file(home: Path, uv: FakeUv) -> None:
    uv.installed = "1.0.0"
    assert _uv_tool.uninstall(home) == [f"legion command removal scheduled. Check the result in {home / _uv_tool.HANDOVER_LOG}."]
    assert uv.detached == [(["uv", "tool", "uninstall", "legion"], [_stop_event.stop_path(home)])]
    assert uv.calls == [["tool", "list"]]
    assert _stop_event.stop_path(home).is_file()


def test_uninstall_waits_for_running_legion_and_hands_nothing_over_on_a_timeout(home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    uv.installed = "1.0.0"
    monkeypatch.setattr(_stop_event, "stop_all", lambda _home, **_options: [77])
    with pytest.raises(_uv_tool.UvToolError, match="process 77"):
        _uv_tool.uninstall(home)
    assert uv.detached == []


def test_uninstall_of_a_missing_tool_does_nothing(home: Path, uv: FakeUv) -> None:
    assert _uv_tool.uninstall(home) == []
    assert uv.detached == []
    assert not _stop_event.stop_path(home).exists()


def test_uninstall_dry_run_shows_the_command_and_starts_nothing(home: Path, uv: FakeUv) -> None:
    uv.installed = "1.0.0"
    assert _uv_tool.uninstall(home, dry_run=True) == [_uv_tool.STOP_NOTICE, "uv tool uninstall legion"]
    assert uv.detached == []
    assert not _stop_event.stop_path(home).exists()


# uv is a global helper like git: no command legion runs ever removes or replaces it.
def test_no_legion_command_touches_uv_itself(tmp_path: Path, home: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch) -> None:
    uv.installed = "0.9.0"
    _uv_tool.install(make_source(tmp_path), home)
    monkeypatch.setattr(_uv_tool, "sys", SimpleNamespace(platform="win32", prefix=str(uv.tools / "legion")))
    _uv_tool.install(make_source(tmp_path / "again", "2.0.0"), home)
    _uv_tool.uninstall(home)
    for arguments in [*uv.calls, *(command[1:] for command, _remove in uv.detached)]:
        assert arguments[0] in {"tool", "export"}
        assert arguments[:2] != ["tool", "uninstall"] or arguments[2:] == ["legion"]
        assert "self" not in arguments
