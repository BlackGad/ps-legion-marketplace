# Run commands with stdin closed, with live output or captured output and a timeout.

import ctypes
import os
import re
import shlex
import signal
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from legion.core._home import resolve_directory, same_path

# After a timeout kill, how long to wait for the pipes to close; a process that left the tree can hold them.
DRAIN_SECONDS = 5


def display_command(words: Sequence[str]) -> str:
    return subprocess.list2cmdline(words) if sys.platform == "win32" else shlex.join(words)


def launch_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    if sys.prefix != sys.base_prefix and "PATH" in environment:
        scripts = resolve_directory(Path(sysconfig.get_path("scripts")))
        environment["PATH"] = os.pathsep.join(entry for entry in environment["PATH"].split(os.pathsep) if not entry or not same_path(resolve_directory(Path(entry)), scripts))
    return environment


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int
    output: str
    error: str
    timed_out: bool


def _native_command(command: str, arguments: Sequence[str]) -> str | list[str]:
    words = [command, *arguments]
    if sys.platform == "win32" and Path(command).suffix.lower() in {".cmd", ".bat"}:
        return _cmd_line(words)
    return words


def run_foreground(command: str, arguments: Sequence[str]) -> int:
    # Keep the terminal on stdout and stderr so the program can draw its own progress.
    return subprocess.run(_native_command(command, arguments), stdin=subprocess.DEVNULL, check=False).returncode


# Return exit code -1 and timed_out when the timeout kills the process tree.
# A .cmd or .bat runs through cmd.exe with every character kept literal; a line break cannot pass and raises ValueError.
def run_command(
    command: str,
    arguments: Sequence[str],
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> RunResult:
    if progress is not None and timeout is not None:
        raise ValueError("Live output requires a command without a timeout")
    on_windows = sys.platform == "win32"
    process = subprocess.Popen(
        _native_command(command, arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if progress is not None else subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=not on_windows,
        # Suspended until it is in the job, so no process it starts can escape the timeout.
        creationflags=subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED if sys.platform == "win32" else 0,
    )
    job = None
    if on_windows:
        try:
            job = _windows_job(process.pid)
        except OSError:
            process.kill()
            process.communicate()
            raise
    try:
        if progress is not None:
            assert process.stdout is not None
            try:
                with process.stdout:
                    for line in process.stdout:
                        progress(_text(line).rstrip("\r\n"))
                return RunResult(exit_code=process.wait(), output="", error="", timed_out=False)
            except BaseException:
                _kill_tree(process, job)
                process.wait()
                raise
        try:
            output, error = process.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(process, job)
            try:
                output, error = process.communicate(timeout=DRAIN_SECONDS)
            except subprocess.TimeoutExpired:
                # A descendant can still hold the pipes after the first stop; try the tree again.
                _kill_tree(process, job)
                try:
                    output, error = process.communicate(timeout=DRAIN_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    output, error = b"", b""
            timed_out = True
    finally:
        if job:
            _close_job(job)
    return RunResult(exit_code=-1 if timed_out else process.returncode, output=_text(output), error=_text(error), timed_out=timed_out)


# cmd.exe reads the whole line: quotes keep & | < > ^ literal, /v:off keeps !, and %cd:~,% (an empty
# slice of a variable that always exists) stops %NAME% expansion. Backslashes before a quote are doubled for the C runtime.
def _cmd_line(parts: Sequence[str]) -> str:
    quoted = []
    for part in parts:
        if "\n" in part or "\r" in part:
            raise ValueError("cmd.exe cannot pass a line break in an argument")
        doubled = re.sub(r'(\\+)(?="|\Z)', r"\1\1", part)
        quoted.append('"' + doubled.replace('"', '""').replace("%", "%%cd:~,%") + '"')
    return f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /v:off /s /c "{" ".join(quoted)}"'


def _text(data: bytes) -> str:
    # Children (legion, uv, agent CLIs) write UTF-8; the console code page would garble it.
    return data.decode("utf-8", errors="replace")


def _kill_tree(process: subprocess.Popen[bytes], job: Optional[int]) -> None:
    # The job or process group still contains descendants after the original child exits.
    if sys.platform == "win32":
        if not job or not ctypes.WinDLL("kernel32").TerminateJobObject(ctypes.c_void_p(job), 1):
            process.kill()
        return
    # The child leads its own session, so its process group holds every descendant that did not leave it.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS answers EPERM for a group of only zombies when the exit races the timeout; a live leader is a real denial.
        if process.poll() is None:
            raise


_CREATE_SUSPENDED = 0x00000004
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_SUSPEND_RESUME = 0x0800


# Put the suspended process in a job, so a timeout can end its children after their parent exits, then let it run.
def _windows_job(pid: int) -> int:
    assert sys.platform == "win32"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError("could not create a job for the process")
    process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME, False, pid)
    try:
        if not process or not _assign_to_job(job, process):
            _close_job(job)
            raise OSError("could not put the process in a job")
        if ntdll.NtResumeProcess(process) != 0:
            _close_job(job)
            raise OSError("could not start the process in its job")
    finally:
        if process:
            kernel32.CloseHandle(process)
    return job


def _assign_to_job(job: int, process: int) -> bool:
    assert sys.platform == "win32"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    return bool(kernel32.AssignProcessToJobObject(job, process))


def _close_job(job: int) -> None:
    assert sys.platform == "win32"
    ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(job))
