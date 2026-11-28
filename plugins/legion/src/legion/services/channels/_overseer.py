# Account choice, saved launches and interactive overseer sessions.
# Hand-built, reason: Legion's launch/resume policy is application behavior; no library supplies it.

import base64
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

import click
from pydantic import BaseModel, Field, TypeAdapter

from legion.services.channels import _bridge, _channel
from legion.services.repository import _lease, _repository
from legion.core import (
    _home,
    _run,
    _settings_io,
    _stop_event,
    _validate,
)
from legion.core._atomic_write import atomic_write_bytes
from legion.services.accounts._accounts import get_tool
from legion.services.accounts._launch import run_account
from legion.core._settings_models import Agent, PersonalisationSettings

WINDOWS = sys.platform == "win32"
SETUP_QUESTION = "This repository is not set up. Set it up (private)? [Y/n]"
PARENT_VARIABLES = ("NO_COLOR", "TERM", "CLAUDECODE", "CLAUDE_CODE_*", "LEGION_ROLE", "LEGION_TASK", "TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN")
CONVERSATION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class OverseerError(Exception):
    pass


class LastLaunch(BaseModel):
    account: str
    channels: list[str] = Field(default_factory=list)
    arguments: list[str] = Field(default_factory=list)
    session: str = ""
    previous_sessions: dict[str, str] = Field(default_factory=dict)


# The account starts through legion's own account launch, in this terminal or in a new Terminal tab.
class Launch(BaseModel):
    account: str
    home: str
    arguments: list[str]
    environment: dict[str, str] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)
    bridge: list[str] = Field(default_factory=list)
    bot_folder: Optional[str] = None


_HISTORY = TypeAdapter(dict[str, LastLaunch])


def interactive() -> bool:
    return sys.stdin.isatty()


def repo_key(repo: Path) -> str:
    return os.path.normcase(str(_home.resolve_directory(repo)))


def _history_file(home: Path) -> Path:
    return home / ".legion/state/overseer-launches.json"


def _history(home: Path) -> dict[str, LastLaunch]:
    path = _history_file(home)
    return _HISTORY.validate_json(_validate.read_text(path)) if path.exists() else {}


def load_last(repo: Path, home: Path) -> Optional[LastLaunch]:
    return _history(home).get(repo_key(repo))


def save_last(repo: Path, home: Path, last: LastLaunch) -> None:
    saved = _history(home)
    saved[repo_key(repo)] = last
    atomic_write_bytes(_history_file(home), _HISTORY.dump_json(saved, indent=2) + b"\n", keep_mode=False)


def _select(settings: PersonalisationSettings, account: Optional[str], tool: Optional[str], channels: tuple[str, ...], last: Optional[str]) -> Agent:
    if account:
        selected = _channel._agent(settings, account)
        if tool and selected.tool != tool:
            raise OverseerError(f"account '{account}' is a {selected.tool} account, not {tool}")
        return selected
    eligible = [agent for agent in settings.agents if not tool or agent.tool == tool]
    for channel in channels:
        bound = _channel.binding(settings, channel).accounts
        eligible = [agent for agent in eligible if agent.id in bound]
    if not settings.agents:
        raise OverseerError("No accounts yet. Add one: legion add claude")
    if not eligible:
        raise OverseerError("no account fits; run legion setup first")
    if len(eligible) == 1:
        return eligible[0]
    if not interactive():
        raise OverseerError("Choose an account: --account " + " | ".join(agent.id for agent in eligible))
    for number, agent in enumerate(eligible, 1):
        line = f"{number}) {agent.id} ({agent.tool})"
        bound = [channel.id for channel in settings.channels if agent.id in channel.accounts]
        if bound:
            line += " channels: " + ", ".join(bound)
        if agent.id == last:
            line += " (last)"
        click.echo(line)
    while True:
        number = click.prompt("Which account leads?", type=int)
        if 1 <= number <= len(eligible):
            return eligible[number - 1]


def _default_channel(settings: PersonalisationSettings, agent: Agent) -> tuple[str, ...]:
    fitting = [channel.id for channel in settings.channels if agent.id in channel.accounts and agent.tool in _channel._descriptor(channel.kind).launch]
    return (fitting[0],) if len(fitting) == 1 else ()


def _session(saved_session: str, agent: Agent, home: Path) -> tuple[str, bool]:
    if agent.tool in ("codex", "copilot") and CONVERSATION_ID.fullmatch(saved_session):
        return saved_session, True
    if agent.tool == "codex":
        return "last", bool(saved_session)
    if agent.tool != "claude":
        return "", False
    projects = _home.home_path(agent.config_dir, home) / "projects"
    # Compare names rather than interpolating the session into a wildcard pattern.
    if saved_session and projects.is_dir() and any((folder / f"{saved_session}.jsonl").is_file() for folder in projects.iterdir()):
        return saved_session, True
    return str(uuid.uuid4()), False


def _resume_arguments(tool: str, session: str) -> list[str]:
    if tool == "claude":
        return ["--resume", session]
    if tool == "copilot":
        return [f"--resume={session}"]
    return ["resume", "--last" if session == "last" else session]


def _launch(settings: PersonalisationSettings, agent: Agent, channels: tuple[str, ...], arguments: tuple[str, ...], session: str, resume: bool, repo: Path, home: Path) -> Launch:
    tool = get_tool(agent.tool)
    full_home = str(_home.full_path(home))
    launch = Launch(account=agent.id, home=full_home, arguments=[], environment={"LEGION_HOME": full_home})
    bindings = [_channel.binding(settings, channel) for channel in channels]
    if bindings and not set.intersection(*(set(binding.accounts) for binding in bindings)):
        raise OverseerError("selected channels cannot share one launch")
    kinds: set[str] = set()
    stateful: list[str] = []
    channel_args: list[str] = []
    for binding in bindings:
        descriptor = _channel._descriptor(binding.kind)
        if agent.tool not in descriptor.launch:
            raise OverseerError(f"channel '{binding.id}' does not support tool '{agent.tool}'")
        if agent.id not in binding.accounts:
            raise OverseerError(f"account '{agent.id}' is not bound to channel '{binding.id}'")
        _channel._bindable_agent(settings, agent.id, descriptor)
        if binding.kind not in kinds:
            channel_args.extend(descriptor.launch[agent.tool])
            kinds.add(binding.kind)
        state = (descriptor.state or {}).get(agent.tool)
        if state is None:
            continue
        if binding.id not in stateful:
            stateful.append(binding.id)
        if len(stateful) > 1:
            raise OverseerError(f"A session can use only one {binding.kind} channel (one bot folder per session); asked for: {', '.join(stateful)}")
        folder = home / ".legion" / state.folder / binding.id
        if not (folder / state.token_file).is_file():
            click.echo(_channel.bot_folder_line(binding.id, False, state.setup), err=True)
        launch.environment[state.variable] = str(folder)
        launch.remove.extend(name for name in state.remove if name not in launch.remove)
        if agent.tool == "codex" and binding.kind == "telegram":
            launch.bot_folder = str(folder)
            launch.bridge = [sys.executable, "-m", "legion.cli", "--home", str(home), "telegram", "bridge", "--channel", binding.id, "--repo", str(repo)]
    if resume:
        launch.arguments.extend(_resume_arguments(agent.tool, session))
    elif agent.tool == "claude" and session:
        launch.arguments.extend(["--session-id", session])
    launch.arguments.extend([*arguments, *channel_args, *tool.overseer_args])
    if agent.tool == "claude" and "--channels" in launch.arguments:
        launch.arguments.append("--")
    launch.arguments.append("continue as the overseer")
    return launch


def _environment(launch: Launch) -> dict[str, str]:
    patterns = (*PARENT_VARIABLES, *launch.remove)
    environment = {name: value for name, value in _run.launch_environment().items() if not any(fnmatch.fnmatchcase(name.upper(), pattern.upper()) for pattern in patterns)}
    return environment | launch.environment


def start_bridge(launch: Launch, repo: Path) -> Optional[subprocess.Popen[bytes]]:
    if not launch.bridge or launch.bot_folder is None:
        return None
    folder = Path(launch.bot_folder)
    with (folder / "bridge.log").open("ab") as output, (folder / "bridge.err").open("ab") as error:
        # Inherit the tab's console so closing it also ends the bridge, even without parent cleanup.
        return subprocess.Popen(launch.bridge, cwd=repo, env=_environment(launch), stdin=subprocess.DEVNULL, stdout=output, stderr=error)


def stop_bridge(process: Optional[subprocess.Popen[bytes]], folder: Path) -> None:
    if process is None:
        return
    # Direct Python bridge has no uv parent or descendants; kill only the process this launch started.
    try:
        process.kill()
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    pid_file = folder / "bridge.pid"
    if not _bridge._live_pid(pid_file, _bridge.process_alive):
        pid_file.unlink(missing_ok=True)


def start_launch(repo: Path, launch: Launch, *, in_place: bool = False) -> int:
    # Hand-built, reason: Terminal rejoins arguments without escaping embedded quotes. URL-safe base64
    # carries the JSON as one word; literal semicolons still use the current terminal.
    terminal = None
    if WINDOWS and not in_place and ";" not in str(repo) + launch.model_dump_json():
        terminal = shutil.which("wt")
    if terminal:
        payload = base64.urlsafe_b64encode(launch.model_dump_json().encode("utf-8")).decode("ascii")
        return subprocess.run([terminal, "new-tab", "-d", str(repo), sys.executable, "-m", "legion.services.channels._overseer", str(repo), payload], check=False).returncode
    process = start_bridge(launch, repo)
    try:
        return run_account(launch.account, launch.arguments, Path(launch.home), cwd=repo, environment=_environment(launch))
    finally:
        if launch.bot_folder is not None:
            stop_bridge(process, Path(launch.bot_folder))


def _prepare_repository(repo: Path, home: Path) -> Optional[Path]:
    try:
        repo = _repository._code_root(repo)
    except _repository.RepositoryError:
        raise OverseerError("not a git repository; run legion inside one") from None
    if not _home.settings_path(home).is_file():
        raise OverseerError("No accounts yet. Add one: legion add claude")
    project = repo / ".legion/configuration/project.json"
    if not project.exists():
        if not interactive():
            raise OverseerError(SETUP_QUESTION)
        answer = click.prompt(SETUP_QUESTION, default="", show_default=False).strip().lower()
        if answer not in ("", "y", "yes"):
            click.echo("Nothing changed.")
            return None
        for line in _repository.setup(repo, "private", None, None):
            click.echo(line)
    return repo


def run(
    repo: Path,
    home: Path,
    *,
    account: Optional[str] = None,
    tool: Optional[str] = None,
    channels: tuple[str, ...] = (),
    arguments: tuple[str, ...] = (),
    no_channel: bool = False,
    pick: bool = False,
) -> int:
    prepared = _prepare_repository(repo, home)
    if prepared is None:
        return 0
    repo = prepared
    settings = _settings_io.load(home)
    project = repo / ".legion/configuration/project.json"
    issues = _validate.validate("project", project)
    if issues:
        raise OverseerError("; ".join(issues))
    owner_launch = not account
    bare = not (account or tool or channels or arguments)
    known = {agent.id for agent in settings.agents}
    last = load_last(repo, home)
    saved_account = last.account if last is not None and last.account in known else None
    if bare and not pick:
        account = saved_account
        if account is None:
            lease = _lease.state(repo).lease
            if lease is not None and lease.account in known:
                account = lease.account
    selected = _select(settings, account, tool, channels, saved_account)
    if bare and selected.id == saved_account and last is not None:
        channels = () if no_channel else tuple(last.channels)
        arguments = tuple(last.arguments)
        no_channel = not channels
    if not channels and not no_channel:
        channels = _default_channel(settings, selected)
    sessions = dict(last.previous_sessions) if last is not None else {}
    if last is not None and last.session:
        sessions[last.account] = last.session
    session, resume = _session(sessions.pop(selected.id, ""), selected, home)
    launch = _launch(settings, selected, channels, arguments, session, resume, repo, home)
    for folder in launch.environment.values():
        Path(folder).mkdir(parents=True, exist_ok=True)
    saved = LastLaunch(account=selected.id, channels=list(channels), arguments=list(arguments), session=session, previous_sessions=sessions)
    save_last(repo, home, saved)
    click.echo(f"{'resuming' if resume else 'starting'} overseer: {selected.id}, channel {', '.join(channels) or 'none'}")
    code = start_launch(repo, launch, in_place=owner_launch)
    if _stop_event.stop_raised(home):
        return code
    if code:
        save_last(repo, home, saved.model_copy(update={"session": ""}))
        click.echo(f"the launch failed with exit code {code}", err=True)
    else:
        click.echo(f"launched {selected.id} ({selected.tool}) in {repo}" + "".join(f" channel {channel}" for channel in channels))
    return code


# The new terminal runs this already prepared launch; selection and saving happen only in its parent.
if __name__ == "__main__":
    raise SystemExit(start_launch(Path(sys.argv[1]), Launch.model_validate_json(base64.urlsafe_b64decode(sys.argv[2])), in_place=True))
