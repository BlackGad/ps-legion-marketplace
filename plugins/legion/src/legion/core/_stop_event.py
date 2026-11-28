# Every long-lived legion process (each `legion mcp` server, each `legion run` wrapper) registers a file holding its process id
# in <home>/.legion/run/. An update or a full uninstall writes the stop file there; each registered process sees it within
# a second and exits, so uv can replace legion's files. Only the standard library and psutil are used.

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import psutil

from legion.core import _process
from legion.core._atomic_write import atomic_write_bytes
from legion.core._home import owner_path_safe, run_folder

STOP_FILE = "stop"
CHECK_SECONDS = 1.0
WAIT_SECONDS = 30.0
# A stop file this old is the leftover of an update that died; it no longer stops anything.
STOP_VALID_SECONDS = 600.0


def stop_path(home: Path) -> Path:
    return run_folder(home) / STOP_FILE


def stop_raised(home: Path, *, pid: int | None = None) -> bool:
    path = stop_path(home)
    try:
        if time.time() - path.stat().st_mtime >= STOP_VALID_SECONDS:
            return False
        return pid is None or str(pid).encode("ascii") not in path.read_bytes().split()[1:]
    except OSError:
        return False


# Registers this process until the block ends and calls `on_stop` once from a helper thread when the stop file appears.
@contextlib.contextmanager
def registered(home: Path, on_stop: Callable[[], None], *, wait_for_stop: bool = False) -> Iterator[None]:
    pid = os.getpid()
    registration = run_folder(home) / f"{pid}.pid"
    # Written whole, so a reader never sees an empty file.
    atomic_write_bytes(registration, str(os.getpid()).encode("ascii"), keep_mode=False)
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(CHECK_SECONDS):
            if stop_raised(home, pid=pid):
                on_stop()
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        if wait_for_stop:
            watcher.join()
        registration.unlink(missing_ok=True)


def raise_stop(home: Path, protected: set[int] | None = None) -> None:
    # Store the exclusions for the entire handover, including after the updater exits.
    contents = "\n".join(map(str, [os.getpid(), *sorted(protected or ())]))
    atomic_write_bytes(stop_path(home), contents.encode("ascii"), keep_mode=False)


def clear_stop(home: Path) -> None:
    stop_path(home).unlink(missing_ok=True)


# The registration of a dead process or a reused pid is removed.
def _registered_processes(home: Path, *, discard_stale: bool = True) -> list[psutil.Process]:
    alive: list[psutil.Process] = []
    for registration in sorted(run_folder(home).glob("*.pid")):
        try:
            pid = int(registration.read_text(encoding="ascii"))
            written = datetime.fromtimestamp(registration.stat().st_mtime, UTC)
            if pid > 0:
                process = psutil.Process(pid)
                started = datetime.fromtimestamp(process.create_time(), UTC)
                if (started <= written or _process.same_start_time(started, written)) and _still_running(process):
                    alive.append(process)
                    continue
        except (OSError, ValueError, OverflowError, psutil.Error):
            pass
        if discard_stale:
            # On Windows a process can be deleting its own registration at the same time.
            with contextlib.suppress(PermissionError):
                registration.unlink(missing_ok=True)
    return alive


def running(home: Path) -> list[int]:
    return [process.pid for process in _registered_processes(home)]


def has_registered_ancestor(home: Path, *, pid: int | None = None) -> bool:
    if not owner_path_safe(run_folder(home), home):
        return False
    ancestors = {process.pid for process in _process.ancestors(os.getpid() if pid is None else pid)}
    return any(process.pid in ancestors for process in _registered_processes(home, discard_stale=False))


def _still_running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _stop_targets(registered: list[psutil.Process], protected: set[int]) -> set[psutil.Process]:
    targets = {process for process in registered if process.pid not in protected}
    if sys.platform != "win32":
        return targets
    # Snapshot launchers before shutdown: legion.exe can still lock the tool after Python exits.
    for process in tuple(targets):
        with contextlib.suppress(psutil.Error):
            for parent in process.parents():
                name = parent.name().lower()
                if name == "legion.exe":
                    if parent.pid not in protected:
                        targets.add(parent)
                    break
                if not name.startswith("python"):
                    break
    return targets


# Keep waiting for processes that remove their registration while shutting down.
def stop_all(home: Path, timeout: float = WAIT_SECONDS, poll: float = 0.2, *, updating: bool = False) -> list[int]:
    caller = os.getpid()
    ancestors = {process.pid for process in _process.ancestors(caller)}
    protected = {caller, *ancestors}
    registered_processes = _registered_processes(home)
    alive = _stop_targets(registered_processes, protected)
    raise_stop(home, protected)
    deadline = time.monotonic() + timeout
    registered_processes += _registered_processes(home)
    while True:
        if updating and ancestors.intersection(process.pid for process in registered_processes):
            print("Restart this session after the legion update.", file=sys.stderr, flush=True)
            updating = False
        alive.update(_stop_targets(registered_processes, protected))
        if not alive:
            return []
        while alive:
            alive = {process for process in alive if _still_running(process)}
            remaining = deadline - time.monotonic()
            if remaining <= 0 and alive:
                return sorted(process.pid for process in alive)
            if alive:
                time.sleep(min(poll, remaining))
        registered_processes = _registered_processes(home)
