import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app_fakes import make_link

from legion.services.installation import _handover, _uv_tool
from legion.services.status import status

WINDOWS = sys.platform == "win32"


@pytest.mark.skipif(not WINDOWS, reason="Windows locked files")
def test_purge_retries_after_a_locked_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "locked"
    file.touch()
    pauses: list[int] = []
    remove = _handover._remove

    def remove_after_pause(path: Path) -> None:
        if not pauses:
            raise PermissionError("file is locked")
        remove(path)

    monkeypatch.setattr(_handover, "_remove", remove_after_pause)
    monkeypatch.setattr(_handover.time, "sleep", pauses.append)
    _handover.purge_folder(file)
    assert pauses == [1]
    assert not file.exists()


@pytest.mark.parametrize("root_link", [False, True])
def test_cleanup_only_purge_spares_link_targets(tmp_path: Path, root_link: bool) -> None:
    legion = tmp_path / ".legion %PATH%"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("owner", encoding="utf-8")
    make_link(legion if root_link else legion / "configuration/link", outside)
    code = "import sys; from pathlib import Path; from legion.services.installation import _handover; _handover.start([], [], purge=[Path(sys.argv[1])], log=None)"
    subprocess.run([sys.executable, "-c", code, str(legion)], check=True, timeout=60)
    deadline = time.monotonic() + 60
    while os.path.lexists(legion) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not os.path.lexists(legion)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "owner"


def test_start_runs_base_python_detached_and_prepares_the_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(_handover.subprocess, "Popen", lambda command, **options: started.append((command, options)))
    log = tmp_path / ".legion/temp/uv-tool.log"
    stop = tmp_path / "stop"
    _handover.start(["uv", "tool", "uninstall", "legion"], [stop], purge=[tmp_path / ".legion"], log=log)
    ((command, options),) = started
    assert command[:3] == [getattr(sys, "_base_executable", sys.executable), "-I", "-c"]
    assert json.loads(command[4]) == [[os.getpid()], _handover.UV_WAIT_SECONDS, [str(stop)], [str(tmp_path / ".legion")], ["uv", "tool", "uninstall", "legion"], str(log), None]
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert log.is_file()
    assert ("creationflags" in options) if WINDOWS else options["start_new_session"] is True


def test_cleanup_only_purge_starts_when_it_cannot_create_the_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / ".legion"
    folder.mkdir()
    (folder / "temp").touch()
    started: list[list[str]] = []
    monkeypatch.setattr(_handover.subprocess, "Popen", lambda command, **_options: started.append(command))

    _handover.start([], [], purge=[folder], log=folder / "temp/uv-tool.log")

    (command,) = started
    assert json.loads(command[4])[-4:] == [[str(folder)], [], None, None]


@pytest.mark.parametrize(("exit_code", "purge"), [(0, False), (2, False), (0, True), (2, True), (None, True), ("missing", True)])
def test_uv_handover_waits_for_exit_logs_the_result_and_cleans_up(tmp_path: Path, exit_code: int | str | None, purge: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "constraints %PATH%"
    folder.mkdir()
    stop = tmp_path / "stop"
    stop.touch()
    log = tmp_path / ".legion/temp/uv-tool.log"
    ready = tmp_path / "ready"
    purge_root = log.parent.parent
    result_file = tmp_path / "uv ran"
    code = """import sys
from pathlib import Path
from legion.services.installation import _handover
log, folder, stop, ready, result_file = map(Path, sys.argv[1:6])
command = [
    sys.executable, "-c",
    "import sys; from pathlib import Path; Path(sys.argv[2]).write_text(sys.argv[3]); "
    "print('uv output', flush=True); print('uv error', file=sys.stderr, end=''); sys.exit(int(sys.argv[1]))",
    sys.argv[6], str(result_file), "legion[mcp] & 100% !x!",
]
if sys.argv[6] == "None":
    _handover.UV_WAIT_SECONDS = 3
if sys.argv[6] == "missing":
    command = [str(folder / "missing-uv")]
_handover.start(command, [folder, stop], purge=[log.parent.parent] if sys.argv[7] == "True" else [], log=log)
ready.touch()
sys.stdin.read()
"""
    with subprocess.Popen([sys.executable, "-c", code, str(log), str(folder), str(stop), str(ready), str(result_file), str(exit_code), str(purge)], stdin=subprocess.PIPE) as parent:
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists()
            time.sleep(1.5)
            assert folder.is_dir()
            assert stop.is_file()
            assert "uv output" not in log.read_text(encoding="utf-8")
            if exit_code is None:
                deadline = time.monotonic() + 10
                while (folder.exists() or stop.exists()) and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert not folder.exists()
                assert not stop.exists()
                assert parent.poll() is None
        finally:
            parent.communicate(timeout=10)
    deadline = time.monotonic() + 10
    while (folder.exists() or stop.exists() or (purge and exit_code == 0 and purge_root.exists())) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not folder.exists()
    assert not stop.exists()
    assert purge_root.exists() is (not purge or exit_code != 0)
    if isinstance(exit_code, int):
        assert result_file.read_text() == "legion[mcp] & 100% !x!"
    if not purge_root.exists():
        return
    output = log.read_text(encoding="utf-8")
    if exit_code is None:
        assert "Timed out waiting for the legion command to exit" in output
        assert "uv output" not in output
    elif exit_code == "missing":
        assert "legion handover failed:" in output
        assert not result_file.exists()
    else:
        assert "uv output" in output
        assert "uv error" in output
        assert f"exited {exit_code}" in output
    monkeypatch.setattr(_uv_tool, "find_uv", lambda: None)
    warnings = status(tmp_path, tmp_path).warnings
    assert any("Legion handover failed" in line and str(log) in line for line in warnings) is (exit_code != 0)
