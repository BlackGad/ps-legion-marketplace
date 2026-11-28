# Run the detached work with base Python so uv can replace Legion's environment.

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import TextIO

# Base Python runs this file as a script without Legion installed.
if __name__ == "__main__":
    _process = None
else:
    from legion.core import _process

if sys.platform == "win32":
    import _winapi

UV_WAIT_SECONDS = 60
WINDOWS_DELETE_ATTEMPTS = 30
FAILURE_PREFIX = "legion handover failed:"


def start(command: list[str], cleanup: list[Path], *, purge: list[Path], log: Path | None, receipt: tuple[Path, str] | None = None) -> None:
    if not (command or purge):
        return
    python = getattr(sys, "_base_executable", sys.executable)
    code = Path(__file__).read_text(encoding="utf-8")
    if log is not None:
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_bytes(b"")
        except OSError:
            if command:
                raise
            log = None
    args = json.dumps([_parent_pids(), UV_WAIT_SECONDS, list(map(str, cleanup)), list(map(str, purge)), command, str(log) if log else None, (str(receipt[0]), receipt[1]) if receipt else None])
    program = [python, "-I", "-c", code, args]
    if sys.platform == "win32":
        subprocess.Popen(program, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(program, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _parent_pids() -> list[int]:
    pid = os.getpid()
    if sys.platform == "win32" and _process is not None:
        # uv's launcher can still have legion.exe open after its Python exits.
        for parent in _process.ancestors(pid):
            name = parent.name.lower()
            if name == "legion.exe":
                return [pid, parent.pid]
            if not name.startswith("python"):
                break
    return [pid]


def _wait_for_exit(pid: int, deadline: float) -> None:
    if sys.platform == "win32":
        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as error:
            if error.winerror == 87:  # The process has already exited.
                return
            raise
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if _winapi.WaitForSingleObject(handle, int(remaining * 1000)) == _winapi.WAIT_OBJECT_0:
                return
        finally:
            _winapi.CloseHandle(handle)
    else:
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    raise TimeoutError("Timed out waiting for the legion command to exit")


def _remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.is_junction():
        path.rmdir()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def purge_folder(path: Path) -> None:
    if path.parent.is_symlink() or path.parent.is_junction():
        raise OSError(f"Parent folder is a link: {path.parent}")
    attempts = WINDOWS_DELETE_ATTEMPTS if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            _remove(path)
            return
        except OSError as error:
            if isinstance(error, FileNotFoundError) and not os.path.lexists(path):
                return
            if attempt == attempts - 1:
                raise
            time.sleep(1)


def _run(pids: list[int], timeout: float, command: list[str], cleanup: list[str], stream: TextIO) -> int:
    try:
        deadline = time.monotonic() + timeout
        for pid in pids:
            _wait_for_exit(pid, deadline)
        if command:
            result = subprocess.run(command, stdout=stream, stderr=stream, check=False)
            outcome = f"uv tool command exited {result.returncode}"
            print(f"\n{FAILURE_PREFIX} {outcome}" if result.returncode else f"\n{outcome}", file=stream, flush=True)
            return result.returncode
        print("purging legion folders", file=stream)
        return 0
    except OSError as error:
        print(f"{FAILURE_PREFIX} {error}", file=stream, flush=True)
        return 1
    finally:
        for path in map(Path, cleanup):
            try:
                _remove(path)
            except OSError as error:
                print(f"legion handover could not remove {path}: {error}", file=stream)


def main() -> int:
    pids, timeout, cleanup, purge, command, log, receipt = json.loads(sys.argv[1])
    with ExitStack() as files:
        try:
            stream = files.enter_context(open(log or os.devnull, "a", encoding="utf-8"))
        except OSError:
            stream = files.enter_context(open(os.devnull, "a", encoding="utf-8"))
        result = _run(pids, timeout, command, cleanup, stream)
        if not result and receipt:
            path, content = receipt
            try:
                Path(path).write_text(content, encoding="utf-8")
            except OSError as error:
                print(f"{FAILURE_PREFIX} could not record the installed source: {error}", file=stream)
                result = 1
    if result:
        return result
    for path in map(Path, purge):
        try:
            purge_folder(path)
        except OSError as error:
            result = 1
            if log:
                with suppress(OSError), open(log, "r+", encoding="utf-8") as stream:
                    stream.seek(0, os.SEEK_END)
                    print(f"{FAILURE_PREFIX} could not remove {path}: {error}", file=stream)
    return result


if __name__ == "__main__":
    sys.exit(main())
