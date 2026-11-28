# Start an account's program with its own config folder. The owner's overseer, members, limits,
# inbox nudges and Foundry accounts all launch through here, as `legion run`; no launcher file is written.

import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Optional

import psutil

from legion.core import _process, _settings_io, _stop_event
from legion.core._home import full_path, home_path
from legion.core._run import _cmd_line, launch_environment
from legion.core._settings_models import Agent
from legion.services.accounts._accounts import get_tool
from legion.services.accounts._launchers import LauncherError, command_prefix, target
from legion.services.foundry import recipe
from legion.services.foundry._az import AzAccessToken, check_token
from legion.services.foundry._types import ProviderFile
from legion.services.foundry.copilot import write_provider


@dataclass(frozen=True)
class AccountCommand:
    program: Path
    prefix: list[str]
    set_env: dict[str, str]
    clear_env: list[str] = field(default_factory=list[str])
    provider_file: Optional[tuple[Path, ProviderFile]] = None
    notice: Optional[str] = None

    def words(self, arguments: Sequence[str]) -> list[str]:
        return [str(self.program), *self.prefix, *arguments]

    # The caller's environment (by default this process's, without legion's own virtual environment), changed for the account.
    def environment(self, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        environment = dict(launch_environment() if base is None else base)
        for name in self.clear_env:
            environment.pop(name, None)
        return environment | self.set_env


# The account's own program, or the tool's stock one; the config folder variable comes from the tool, Foundry from its recipe.
def account_command(agent: Agent, home: Path, *, token_check: Callable[[], AzAccessToken] = check_token) -> AccountCommand:
    tool = get_tool(agent.tool)
    program, prefix = command_prefix(target(agent, tool, home))
    legion_home = {"LEGION_HOME": str(full_path(home))}
    if agent.provider is None:
        folder = full_path(home_path(agent.config_dir, home))
        return AccountCommand(program, prefix, {tool.config_env: str(folder), **legion_home})
    launch = recipe(agent).launch(agent, home, token_check())
    return AccountCommand(program, prefix, launch.set | legion_home, launch.clear, launch.provider_file, launch.notice)


# True when this process is in the terminal's foreground group: a Ctrl+C typed there reached the account's program too.
def _foreground() -> bool:
    if sys.platform == "win32":
        return False
    try:
        with open("/dev/tty", "rb") as terminal:
            return os.tcgetpgrp(terminal.fileno()) == os.getpgrp()
    except OSError:
        return False


# A session or thread id as the tools print it, such as "session id: <uuid>" or {"session_id": "<uuid>"}.
SESSION_ID = re.compile(r"(?:session|thread)[ _-]?id\W{1,4}([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", re.IGNORECASE)
READER_SECONDS = 5


# Copies what the account's program prints to ours unchanged, and keeps the last session id it printed.
def _copy(stream: IO[bytes], sink: IO[bytes], session: list[str]) -> None:
    for line in stream:
        sink.write(line)
        sink.flush()
        ids = SESSION_ID.findall(line.decode("utf-8", errors="replace"))
        if ids:
            session[:] = [ids[-1]]


# A terminal stays attached to the program itself; a redirected stream passes through legion, which reads the session id from it.
def _start_readers(child: "subprocess.Popen[bytes]", session: list[str]) -> list[threading.Thread]:
    readers: list[threading.Thread] = []
    for stream, sink in ((child.stdout, sys.stdout), (child.stderr, sys.stderr)):
        if stream is not None:
            reader = threading.Thread(target=_copy, args=(stream, sink.buffer, session), daemon=True)
            reader.start()
            readers.append(reader)
    return readers


# The stop event ends the account's process tree by pid.
def _end_child(child: psutil.Process | None) -> None:
    print("legion: stopped for an update; continue from the handoff afterwards", file=sys.stderr, flush=True)
    if child is not None:
        try:
            _process.end_tree(child)
        except psutil.Error as error:
            print(f"legion: could not stop the account process tree: {error}", file=sys.stderr, flush=True)


# On POSIX a SIGINT or SIGTERM sent to legion's own process is passed on.
def _wait(child: "subprocess.Popen[bytes]") -> int:
    def forward(number: int, _frame: object) -> None:
        if number == signal.SIGINT and _foreground():
            return
        with suppress(ProcessLookupError):
            child.send_signal(number)

    names = [signal.SIGINT, signal.SIGTERM] if sys.platform != "win32" else []
    previous = {number: signal.signal(number, forward) for number in names}
    try:
        while True:
            try:
                code = child.wait()
                # A tool killed by a signal gives -signal; a shell reports 128 + signal.
                return 128 - code if code < 0 else code
            except KeyboardInterrupt:
                if sys.platform != "win32":
                    raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


# Runs the account in this terminal and returns its exit code; Ctrl+C goes to the account's program, not to legion.
# While it runs, this process is registered for the stop event. When the program prints a session id, it is repeated
# on stderr at the end, so the overseer can record it and resume the session later.
def run_account(account: str, arguments: list[str], home: Path, *, cwd: Optional[Path] = None, environment: Optional[Mapping[str, str]] = None) -> int:
    agent = next((item for item in _settings_io.load(home).agents if item.id == account), None)
    if agent is None:
        raise LauncherError(f"unknown account '{account}'")
    command = account_command(agent, home)
    path = command.provider_file[0] if command.provider_file else None
    ready = threading.Event()
    identity: psutil.Process | None = None
    stopped = False

    def stop() -> None:
        nonlocal stopped
        ready.wait()
        stopped = True
        _end_child(identity)

    try:
        with _stop_event.registered(home, stop, wait_for_stop=True):
            try:
                if command.provider_file:
                    write_provider(*command.provider_file)
                if command.notice:
                    print(command.notice, flush=True)
                line: str | list[str] = command.words(arguments)
                if sys.platform == "win32" and Path(line[0]).suffix.lower() in (".cmd", ".bat"):
                    line = _cmd_line(line)
                stdout = None if sys.stdout.isatty() else subprocess.PIPE
                stderr = None if sys.stderr.isatty() else subprocess.PIPE
                child = subprocess.Popen(line, cwd=cwd, env=command.environment(environment), stdout=stdout, stderr=stderr)
                with suppress(psutil.NoSuchProcess):
                    identity = psutil.Process(child.pid)
            finally:
                ready.set()
            session: list[str] = []
            readers = _start_readers(child, session)
            code = _wait(child)
            for reader in readers:
                reader.join(READER_SECONDS)
            if session:
                print(f"legion: session id {session[0]}", file=sys.stderr, flush=True)
        return 0 if stopped else code
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
