# Owner accounts: subscription and Foundry add, register, rename, remove and models.

import os
import re
import shutil
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Optional

from legion.core import _settings_io
from legion.services.accounts import _launchers
from legion.core._generated import shell_literal
from legion.core._home import full_path, home_path, same_path
from legion.core._run import display_command
from legion.core._settings_models import (
    NAME_PATTERN,
    Agent,
    PersonalisationSettings,
    ToolDescriptor,
)
from legion.core._validate import load_descriptors, settings_issues
from legion.services.foundry import RECIPES, _discovery, recipe
from legion.services.lifecycle import _adopt

POSIX = os.name != "nt"


class AccountError(Exception):
    pass


def get_tool(tool_id: str) -> ToolDescriptor:
    matches = [descriptor for descriptor in load_descriptors(ToolDescriptor, "tool.json") if descriptor.id == tool_id]
    if not matches:
        raise AccountError(f"unknown tool '{tool_id}'")
    return matches[0]


def _check_id(account: str) -> None:
    if not re.fullmatch(NAME_PATTERN, account):
        raise AccountError(f"id '{account}' must match ^{NAME_PATTERN}$")


def _find(settings: PersonalisationSettings, account: str) -> Optional[Agent]:
    matches = [agent for agent in settings.agents if agent.id == account]
    return matches[0] if matches else None


def check_new_id(settings: PersonalisationSettings, account: str) -> None:
    _check_id(account)
    if account == "legion":
        raise AccountError(f"'{account}' is reserved for an owner command")
    if _find(settings, account) is not None:
        raise AccountError(f"account '{account}' already exists")
    if _launchers.find_command(account) is not None:
        raise AccountError(f"'{account}' is already a command")


# The owner names a file, or a command on PATH.
def _resolve_command(text: str, account: str, home: Path) -> Path:
    expanded = home_path(text, home)
    if full_path(expanded).is_file():
        command = full_path(expanded)
    else:
        found = shutil.which(str(expanded))
        if found is None:
            raise AccountError(f"target needed: {account}; ask the owner for an existing executable")
        command = full_path(Path(found))
    if POSIX and command.suffix.lower() != ".ps1" and not os.access(command, os.X_OK):
        raise AccountError(f"target is not executable: {command}")
    return command


def _source_folder(config_dir: str, home: Path) -> Path:
    folder = full_path(home_path(config_dir, home))
    if not folder.is_dir():
        raise AccountError("Existing account config folder does not exist.")
    return folder


# The command is stored only when it is not the tool's stock command, which a launch finds on its own.
def _stored_command(command: Optional[Path], tool: ToolDescriptor) -> Optional[str]:
    if command is None or _launchers.is_stock(command, tool):
        return None
    return str(command)


# The command the owner runs once to log in: the account's tool started on its own folder, without legion.
# Windows shows PowerShell, whose single-quoted text doubles a quote; other systems show a POSIX shell.
def login_command(tool: ToolDescriptor, folder: Path, command: Optional[str], home: Path) -> str:
    program = str(full_path(home_path(command, home))) if command is not None else tool.command
    if sys.platform == "win32":
        start = "& '" + program.replace("'", "''") + "'" if command is not None else program
        return f"$env:{tool.config_env}='" + str(folder).replace("'", "''") + f"'; {start}"
    return f"{tool.config_env}={shell_literal(str(folder))} {display_command([program])}"


def _ensure_config_folder(folder: Path) -> list[str]:
    if folder.is_dir():
        return []
    folder.mkdir(parents=True, exist_ok=True)
    return [f"created: config folder {folder}"]


# An account made earlier: a given command replaces the stored one, and a missing config folder is made again.
def _repair(settings: PersonalisationSettings, existing: Agent, tool: ToolDescriptor, command: Optional[Path], folder: Path, home: Path) -> list[str]:
    settings_changed = False
    if command is not None:
        wanted = _stored_command(command, tool)
        settings_changed = existing.command != wanted
        existing.command = wanted
    _launchers.target(existing, tool, home)
    if settings_changed:
        _settings_io.save_locked(home, settings)
    lines = _ensure_config_folder(folder)
    if settings_changed:
        lines.append(f"wrote: command of account {existing.id}")
    return lines or [f"skip: account {existing.id} (already done)"]


# A folder given by the owner is copied into the new account's own folder; the original stays as it is.
def add_subscription(home: Path, account: str, tool_id: str, command: Optional[str], config_dir: Optional[str], say: Callable[[str], None] = lambda _line: None) -> list[str]:
    _check_id(account)
    tool = get_tool(tool_id)
    real = _resolve_command(command, account, home) if command is not None else None
    source = _source_folder(config_dir, home) if config_dir is not None else None
    folder = full_path(home / ".legion" / "agents" / account)
    with _settings_io.writer(home, require_existing=False):
        settings = _settings_io.load_or_empty(home)
        existing = _find(settings, account)
        if existing is not None and existing.provider is None and existing.tool == tool_id:
            own = full_path(home_path(existing.config_dir, home))
            if source is not None and not same_path(source, own):
                raise AccountError(f"account '{account}' already has its folder; legion add {tool_id} <folder> copies a folder into a new account")
            return _repair(settings, existing, tool, real, own, home)
        check_new_id(settings, account)
        if source is not None:
            _adopt.adopt(source, folder, tool, say)
        agent = Agent.model_construct(id=account, tool=tool_id, config_dir=str(folder), command=_stored_command(real, tool))
        _launchers.target(agent, tool, home)
        settings.agents.append(agent)
        try:
            _settings_io.save_locked(home, settings)
        except Exception:
            if source is not None:
                shutil.rmtree(folder, ignore_errors=True)
            raise
        folder.mkdir(parents=True, exist_ok=True)
    if source is not None:
        return [f"Copied {source} into {folder}; open it with 'legion --account {account}'."]
    return [f"Log in once: {login_command(tool, folder, _stored_command(real, tool), home)}"]


def add_foundry(home: Path, account: str, tool_id: str, endpoint: str, names: list[str], default: Optional[str], aliases: Optional[list[str]], use_standard: bool, list_only: bool) -> list[str]:
    _check_id(account)
    tool = get_tool(tool_id)
    if tool.foundry is None or tool_id not in RECIPES:
        raise AccountError(f"tool '{tool_id}' does not support Foundry")
    endpoint = _discovery.endpoint_url(endpoint)
    before = _settings_io.load_or_empty(home)
    existing = _find(before, account)
    if not list_only and existing and existing.tool == tool_id and existing.provider and existing.provider.kind == "foundry" and existing.provider.endpoint == endpoint:
        return [f"skip: account {account} (already done)"]
    if not list_only:
        check_new_id(before, account)
    discovery = _discovery.discover(endpoint, tool)
    if list_only:
        return [_discovery.listing(discovery, []).model_dump_json(indent=2)]
    provider = _discovery.provider_block(endpoint, tool, names, default, aliases, discovery, use_standard=use_standard)
    with _settings_io.writer(home, require_existing=False):
        settings = _settings_io.load_or_empty(home)
        check_new_id(settings, account)
        agent = Agent.model_validate({"id": account, "tool": tool_id, "configDir": str(home / ".legion" / "agents" / account), "provider": provider.model_dump(by_alias=True, exclude_none=True)})
        settings.agents.append(agent)
        _save_foundry_locked(home, settings, agent)
    return [f"Start it with 'legion --account {account}'; it signs in to Azure through az when needed."]


def _save_foundry_locked(home: Path, settings: PersonalisationSettings, agent: Agent) -> None:
    issues = settings_issues(settings)
    if issues:
        raise _settings_io.SettingsError("; ".join(issues))
    recipe(agent).configure(agent, home)
    _settings_io.save_locked(home, settings)


def models(home: Path, account: str, names: list[str], default: Optional[str], aliases: Optional[list[str]], use_standard: bool, list_only: bool) -> list[str]:
    settings = _settings_io.load(home)
    agent = _find(settings, account)
    if agent is None:
        raise AccountError(f"unknown account '{account}'")
    if agent.provider is None or agent.provider.kind != "foundry":
        raise AccountError(f"account '{account}' is not a Foundry agent")
    tool = get_tool(agent.tool)
    discovery = _discovery.discover(agent.provider.endpoint, tool)
    if list_only:
        return [_discovery.listing(discovery, agent.provider.deployments).model_dump_json(indent=2)]
    discovered_endpoint = agent.provider.endpoint
    discovered_tool = agent.tool
    with _settings_io.writer(home):
        settings = _settings_io.load(home)
        agent = _find(settings, account)
        if agent is None or agent.provider is None or agent.provider.kind != "foundry":
            raise AccountError(f"unknown account '{account}'")
        if agent.provider.endpoint != discovered_endpoint or agent.tool != discovered_tool:
            raise AccountError(f"account '{account}' changed during discovery; run the command again")
        provider = _discovery.provider_block(agent.provider.endpoint, tool, names, default, aliases, discovery, agent.provider, use_standard)
        if agent.provider == provider:
            return [f"skip: models {account} (no changes)"]
        agent.provider = provider
        _save_foundry_locked(home, settings, agent)
    return []


# Bindings follow a renamed account; removing an account cannot leave a channel without accounts.
def _rebind(settings: PersonalisationSettings, account: str, new_id: Optional[str]) -> None:
    for channel in settings.channels:
        if new_id is None and channel.accounts == [account]:
            raise AccountError(f"channel '{channel.id}' would have no accounts; rebind or remove it first")
    for channel in settings.channels:
        if new_id is None:
            channel.accounts = [name for name in channel.accounts if name != account]
        else:
            channel.accounts = [new_id if name == account else name for name in channel.accounts]


def rename(home: Path, account: str, new_id: str) -> list[str]:
    with _settings_io.writer(home):
        settings = _settings_io.load(home)
        old = _find(settings, account)
        if old is None and _find(settings, new_id) is not None:
            return [f"skip: account rename {account} to {new_id} (already done)"]
        check_new_id(settings, new_id)
        if old is None:
            raise AccountError(f"unknown account '{account}'")
        _rebind(settings, account, new_id)
        old.id = new_id
        _settings_io.save_locked(home, settings)
    return [f"Config folder kept at {old.config_dir}; rename it yourself only if you want to."]


def remove(home: Path, account: str) -> list[str]:
    with _settings_io.writer(home):
        settings = _settings_io.load_unchecked(home)
        agent = _find(settings, account)
        if agent is None:
            return [f"skip: account remove {account} (already done)"]
        _rebind(settings, account, None)
        settings.agents = [other for other in settings.agents if other.id != account]
        _settings_io.save_locked(home, settings)
    return [f"Config folder kept at {agent.config_dir}."]
