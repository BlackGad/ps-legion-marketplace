# Plan and apply changes to Legion installations and accounts.

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from functools import cmp_to_key, partial
from pathlib import Path
from typing import Literal

import click

from legion.core import _generated, _settings_io, _stop_event
from legion.core._home import (
    full_path,
    home_path,
    is_reparse,
    owner_path_safe,
    path_contains,
    resolve_directory,
    run_folder,
    same_path,
    settings_path,
)
from legion.core._plugin import PLUGIN_ROOT
from legion.core._settings_models import (
    Agent,
    CatalogTool,
    PersonalisationSettings,
    PluginPin,
    ToolsCatalog,
)
from legion.core._validate import read_text
from legion.services.accounts import _accounts, _launchers
from legion.services.installation import (
    _knowledge_hub,
    _tool_install,
    _user_path,
    _uv_tool,
)
from legion.services.installation._update import compare_versions
from legion.services.lifecycle import _plugins, _targets
from legion.services.lifecycle._catalog import DEFAULT_SOURCE, catalog_tool, load_catalog
from legion.services.repository._footprints import FootprintError

Intent = Literal["setup", "install", "update", "finish", "uninstall", "purge"]
Say = Callable[[str], None]
Copy = tuple[str, Path]
ERRORS = (_settings_io.SettingsError, _accounts.AccountError, _launchers.LauncherError, _tool_install.ToolInstallError, FootprintError, OSError, ValueError, TypeError, RuntimeError)
NOTES_TEMPLATE = PLUGIN_ROOT / "skills/legion-personalisation/references/notes-template.md"


@dataclass
class Step:
    line: str
    target: str
    folder: Path
    run: Callable[[Say], str]
    needs_yes: bool = True
    stop_on_failure: bool = False


@dataclass
class Plan:
    intent: Intent
    steps: list[Step] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    dry_run: bool = False
    summary: str | None = None
    stopped_home: Path | None = None
    source_label: str | None = None
    self_update: bool = False
    failed_target: str | None = None


def _approved() -> bool:
    # Read once, including when stdin is a pipe or is closed.
    click.echo("Apply? [y/N]")
    try:
        answer = sys.stdin.readline() if sys.stdin is not None else ""
    except (EOFError, OSError, ValueError):
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def apply(changes: Plan, *, yes: bool = False, say: Say = click.echo) -> int:
    for notice in changes.notices:
        say(notice)
    if not changes.steps:
        if changes.intent != "finish":
            say("Nothing changed (dry run)." if changes.dry_run else changes.summary or "Nothing to do.")
        return 0
    say(f"{changes.intent.capitalize()} plan:")
    if changes.source_label:
        say(f"  Source: {changes.source_label}")
    for step in changes.steps:
        suffix = " (needs approval, run legion setup)" if changes.intent == "finish" and step.needs_yes else ""
        say(f"  {step.line}{suffix}")
    if not (yes or changes.dry_run or changes.intent == "finish") and any(step.needs_yes for step in changes.steps) and not _approved():
        flag = "-Yes" if os.environ.get("LEGION_BOOTSTRAP_POWERSHELL") == "1" else "--yes"
        say(f"Nothing changed. Run again with {flag} to apply.")
        return 0

    rows = [("Target", "Folder", "Result")]
    failed = False
    try:
        for step in changes.steps:
            if changes.intent == "finish" and step.needs_yes:
                continue
            say(step.line)
            try:
                result = step.run(say)
            except ERRORS as error:
                failed = True
                changes.failed_target = step.target
                result = f"failed: {' '.join(str(error).split())}"
                if changes.intent == "finish":
                    click.echo(f"First-run check: {step.line}: {result}", err=True)
            rows.append((step.target, str(step.folder), result))
            if (step.stop_on_failure or changes.self_update) and result.startswith("failed: "):
                break
    finally:
        if changes.stopped_home is not None:
            _stop_event.clear_stop(changes.stopped_home)
            changes.stopped_home = None
    if not changes.dry_run or failed:
        say("Results:")
        target_width = max(len(row[0]) for row in rows)
        folder_width = max(len(row[1]) for row in rows)
        for target, folder, result in rows:
            say(f"{target.ljust(target_width)}  {folder.ljust(folder_width)}  {result}")
        outcome = f"{changes.intent.capitalize()} {'failed' if failed else 'completed'}."
        say(changes.summary if changes.summary and not failed else outcome)
    else:
        say("Nothing changed (dry run).")
    return int(failed)


@contextmanager
def saved_path() -> Iterator[None]:
    before = os.environ.get("PATH")
    try:
        _add_saved_folders()
        yield
    finally:
        if before is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = before


def _add_saved_folders() -> None:
    if sys.platform == "win32":
        path = os.environ.get("PATH", "")
        for folder in _user_path.persistent_folders():
            if not path_contains(path, Path(folder)):
                path = os.pathsep.join(filter(None, (path, folder)))
        os.environ["PATH"] = path


def update_blocked(home: Path) -> bool:
    return sys.platform == "win32" and _stop_event.has_registered_ancestor(home)


def plan(
    home: Path,
    intent: Intent,
    *,
    source: str | Path = DEFAULT_SOURCE,
    source_explicit: bool = False,
    ref: str = "release",
    dry_run: bool = False,
    account_tool: str | None = None,
    account_folder: str | None = None,
    account_id: str | None = None,
    knowledge: bool | None = None,
    self_update: bool = False,
) -> Plan:
    home = full_path(home)
    if intent == "update" and not dry_run and update_blocked(home):
        raise ValueError("Close this Legion session, run `legion update` from a terminal, then reopen Legion.")
    changes = Plan(intent, dry_run=dry_run, self_update=self_update)
    file = settings_path(home)
    if intent == "finish" and not file.exists():
        return changes
    catalog = load_catalog(source, ref)
    try:
        settings = _settings_io.load_unchecked(home) if file.exists() else _settings_io.load_or_empty(home)
    except _settings_io.SettingsError as error:
        if intent not in ("uninstall", "purge"):
            raise ValueError(f"Fix invalid settings in {file}: {error}") from error
        reason = " ".join(str(error).split()).split("; ", 1)[0]
        changes.notices.append(f"skip: account folders (settings invalid: {reason})")
        settings = PersonalisationSettings.model_validate_json(_settings_io.EMPTY)

    if intent in ("uninstall", "purge"):
        _uninstall_steps(changes, home, catalog, settings)
        return changes

    checkout = resolve_directory(Path(source)) if Path(source).is_dir() else None
    if intent != "finish":
        changes.source_label = _source_label(source, ref, checkout, source_explicit)
    roots: list[Copy] = []
    if intent in ("setup", "finish"):
        agents = list(settings.agents)
        requested = None
        given = None
        if account_tool is not None:
            catalog_tool(catalog, account_tool)
            requested, given = _account_request(home, account_tool, account_folder, account_id)
            agents = [agent for agent in agents if agent.id != requested.id] + [requested]
        pending_plugin = _setup_steps(changes, home, catalog.tools, agents, source, roots, requested, given)
        if knowledge is not None:
            line = f"Turn knowledge {'on' if knowledge else 'off'}"
            changes.steps.append(Step(line, "knowledge", file.parent, partial(_set_knowledge, home, catalog, knowledge)))
        enabled = knowledge if knowledge is not None else settings.knowledge_enabled
        _knowledge_steps(changes, home, catalog.tools, agents, catalog.wiki, enabled)
        uv = _uv_tool.find_uv()
        existing = _uv_tool.installed_version(uv) if uv else None
        expected = _newest(roots)[0] if roots else _uv_tool.plugin_version(PLUGIN_ROOT)
        if pending_plugin or existing != expected or (uv and _uv_tool.path_missing(uv)):
            changes.steps.append(_command_step(home, roots, changes, fallback=not agents))
        return changes

    _installation_steps(changes, home, catalog, settings, source, ref, checkout, roots, source_explicit)
    return changes


def _source_label(source: str | Path, ref: str, checkout: Path | None, source_explicit: bool) -> str:
    if checkout:
        return str(checkout)
    if source == DEFAULT_SOURCE and ref == "release" and not source_explicit:
        return f"{source} (stable for new registrations; existing targets use their registered marketplace)"
    return f"{source} ({'main' if ref == 'main' else 'stable'})"


def _installation_steps(
    changes: Plan, home: Path, catalog: ToolsCatalog, settings: PersonalisationSettings, source: str | Path, ref: str, checkout: Path | None, roots: list[Copy], source_explicit: bool
) -> None:
    intent, dry_run = changes.intent, changes.dry_run
    checkout_root = checkout / "plugins/legion" if checkout else None
    checkout_digest = _targets.plugin_digest(checkout_root) if checkout_root else None
    copies: list[Copy] = []
    for entry in catalog.tools:
        folders = _targets.folders(home, entry, settings.agents)
        if not any(command for _, command in folders):
            changes.notices.extend((f"skip: {entry.id} (not installed)", entry.install_hint))
        for folder, command in folders:
            version = _targets.installed_version(folder, entry) or "not installed"
            line = f"{intent.capitalize()} legion for {entry.id} in {folder} ({version})"
            if command is None:
                changes.steps.append(Step(line, entry.id, folder, partial(_removal_error, ValueError(f"{entry.id} command is missing"))))
                continue
            target = entry.model_copy(update={"command": command})
            try:
                before, arguments = _plugins.installation_steps(
                    target, folder, source, ref, updating=intent == "update", checkout=checkout, checkout_digest=checkout_digest, source_explicit=source_explicit
                )
            except ERRORS as error:
                changes.steps.append(Step(line, entry.id, folder, partial(_removal_error, error)))
                continue
            if before:
                copies.append(before)
            action = partial(_install_in, target, folder, before, arguments, roots, dry_run=dry_run, updating=intent == "update", checkout_digest=checkout_digest)
            changes.steps.append(Step(line, entry.id, folder, action, needs_yes=bool(arguments)))
    fallback = not changes.steps
    if fallback:
        changes.notices.insert(0, "No coding CLI is installed. Run legion setup to install one.")
        if source_explicit and not checkout:
            changes.source_label = (changes.source_label or str(source)) + " (no available CLI for this marketplace; the command uses the bundled copy)"
    if intent == "update":
        _knowledge_steps(changes, home, catalog.tools, settings.agents, catalog.wiki, settings.knowledge_enabled)
    uv = _uv_tool.find_uv()
    expected_root = checkout_root or (_newest(copies)[1] if copies else PLUGIN_ROOT)
    receipt = f"{checkout_root}\n{checkout_digest}" if checkout_root else None
    command_needed = uv is None or _uv_tool.replacement_needed(uv, expected_root, home, receipt) or (intent == "install" and _uv_tool.path_repair_needed(uv))
    changes.steps.append(_command_step(home, roots, changes, fallback=fallback, checkout_root=checkout_root, receipt=receipt, needed=command_needed))
    if not any(step.needs_yes for step in changes.steps):
        changes.steps.clear()
        changes.notices.clear()
        if intent == "update":
            changes.summary = "Nothing to do. Legion is already up to date."
        else:
            changes.summary = "Nothing to do. Legion is already installed for the available targets."


def _removal_targets(home: Path, catalog: ToolsCatalog, settings: PersonalisationSettings) -> Iterator[tuple[CatalogTool, Path, str | None, PluginPin | None]]:
    wiki_folders = {(agent.tool, _targets.folder_key(_targets.account_folder(home, agent))) for agent in settings.agents} if settings.knowledge_enabled else set()
    for entry in catalog.tools:
        for folder, command in _targets.folders(home, entry, settings.agents):
            with_wiki = (entry.id, _targets.folder_key(folder)) in wiki_folders and not _targets.is_default_folder(home, entry, folder)
            yield entry, folder, command, catalog.wiki if with_wiki else None


def _uninstall_steps(changes: Plan, home: Path, catalog: ToolsCatalog, settings: PersonalisationSettings) -> None:
    for entry, folder, command, pin in _removal_targets(home, catalog, settings):
        if command is None:
            changes.notices.append(f"skip: {entry.id} in {folder} (no command for this folder)")
            continue
        target = entry.model_copy(update={"command": command})
        for recipe in (None, pin) if pin else (None,):
            plugin = "llm-wiki" if recipe else "legion"
            line = f"Remove {plugin} for {entry.id} in {folder}"
            label = f"{entry.id} knowledge" if recipe else entry.id
            action: Callable[[Say], str]
            try:
                arguments = _plugins.removal_steps(target, folder, recipe)
            except ERRORS as error:
                action = partial(_removal_error, error)
            else:
                if not arguments:
                    changes.notices.append(f"skip: {plugin} for {entry.id} in {folder} (already done)")
                    continue
                action = partial(_remove_from, target, folder, arguments, changes.dry_run)
            changes.steps.append(Step(line, label, folder, action))

    purge = changes.intent == "purge"
    legion = home / ".legion"
    hub = purge and os.path.lexists(legion / "knowledge")
    folders = _purge_folders(home, keep_hub=hub) if purge else []
    path_lines = _user_path.remove(home, purge, dry_run=True)
    uv_lines = _uv_tool.uninstall(home, dry_run=True)
    if not (changes.steps or folders or hub or path_lines or uv_lines):
        changes.notices.append(f"skip: {changes.intent} (already done)")
        return

    stopped = bool(uv_lines) or (owner_path_safe(_stop_event.stop_path(home), home) and run_folder(home).is_dir())
    if stopped:

        def stop(_say: Say) -> str:
            if changes.dry_run:
                return "previewed"
            _uv_tool.stop_running(home)
            changes.stopped_home = home
            return "stopped"

        changes.steps.insert(0, Step("Waiting for running Legion sessions to exit...", "sessions", home, stop, stop_on_failure=True))

    preview = [*path_lines, *uv_lines[1:]]
    if folders or hub:
        preview.append(f"Remove folder: {legion}")
    if hub:
        preview.append("Asked separately: the knowledge hub in ~/.legion/knowledge")
    line = "\n  ".join(preview) or "Finish uninstall"
    changes.steps.append(Step(line, "legion command", home, partial(_removal_handover, changes, home, hub)))


def _removal_error(error: Exception, _say: Say) -> str:
    raise error


def _remove_from(entry: CatalogTool, folder: Path, arguments: list[list[str]], dry_run: bool, say: Say) -> str:
    for command in arguments:
        _plugins.run_step(entry, command, folder, dry_run, say)
    return "previewed" if dry_run else "removed"


def _removal_handover(changes: Plan, home: Path, hub: bool, say: Say) -> str:
    if changes.dry_run:
        return "previewed"
    purge = changes.intent == "purge"
    for line in _user_path.remove(home, purge):
        say(line)
    with_knowledge = False
    if hub:
        with suppress(click.Abort):
            with_knowledge = click.confirm("Also delete the knowledge hub in ~/.legion/knowledge? [y/N]", default=False, prompt_suffix=" ", show_default=False)
    if with_knowledge:
        lines: list[str] = []
        _knowledge_hub.purge(home, lines)
        for line in lines:
            say(line)
    remaining = _purge_folders(home, keep_hub=hub and not with_knowledge) if purge else []
    result = _uv_tool.uninstall(home, stopped=changes.stopped_home is not None, purge=remaining, progress=say)
    if not result:
        return "already done"
    changes.stopped_home = None  # The handover owns the stop file now.
    changes.summary = result[0]
    return "removal scheduled" if "scheduled" in result[0] else "removed"


def _purge_folders(home: Path, *, keep_hub: bool) -> list[Path]:
    legion = home / ".legion"
    if not os.path.lexists(legion):
        return []
    # Handover removes a linked Legion folder as a link.
    if is_reparse(legion) or not keep_hub:
        return [legion]
    return [child for child in legion.iterdir() if child.name != "knowledge"]


def _install_in(
    entry: CatalogTool,
    folder: Path,
    before: Copy | None,
    arguments: list[list[str]],
    roots: list[Copy],
    say: Say,
    *,
    dry_run: bool = False,
    updating: bool = False,
    checkout_digest: str | None = None,
) -> str:
    if not arguments and before:
        roots.append(before)
        say(f"skip: legion {before[0]} in {folder} (already done)")
        return "already done"
    if not dry_run:
        folder.mkdir(parents=True, exist_ok=True)
    before_digest = _targets.plugin_digest(before[1]) if updating and before and not dry_run else None
    for args in arguments:
        _plugins.run_step(entry, args, folder, dry_run, say)
    if dry_run:
        if before:
            roots.append(before)
        return "previewed"
    after = _plugins.verified_copy(entry, folder)
    if after is None:
        raise ValueError(f"{entry.id} legion copy is missing after {'update' if updating else 'install'}")
    if checkout_digest is not None and _targets.plugin_digest(after[1]) != checkout_digest:
        raise ValueError(f"{entry.id} did not install the checkout contents in {folder}")
    roots.append(after)
    version = after[0]
    if updating and before:
        if before[0] != version:
            say(f"legion {before[0]} -> {version}")
            return f"{before[0]} -> {version}"
        update_commands = [args for args in (entry.plugin.marketplace_update, entry.plugin.update) if args]
        if arguments == update_commands and before_digest == _targets.plugin_digest(after[1]):
            say(f"skip: legion {version} in {folder} (already done)")
            return "already done"
    if entry.hooks.hint:
        say(entry.hooks.hint)
    return f"installed legion {version}"


def _newest(roots: list[Copy]) -> Copy:
    return max(roots, key=cmp_to_key(lambda left, right: compare_versions(left[0], right[0])))


def _command_step(home: Path, roots: list[Copy], changes: Plan, *, fallback: bool, checkout_root: Path | None = None, receipt: str | None = None, needed: bool = True) -> Step:
    updating = changes.intent == "update"
    line = "Refresh the legion command with uv (running Legion sessions end before replacement)"
    if not updating:
        line += " and put uv's tool folder on your PATH if needed"
    if _uv_tool.find_uv() is None:
        line += ". " + _uv_tool.UV_MISSING

    def run(say: Say) -> str:
        if not (roots or fallback or changes.dry_run):
            return "skipped (no successful plugin copy)"
        root = checkout_root or (_newest(roots)[1] if roots else PLUGIN_ROOT)
        version = _uv_tool.plugin_version(root)
        outcomes = _uv_tool.install(root, home, dry_run=changes.dry_run, update_shell=not updating, updating=updating, progress=say, receipt=receipt, rollback=changes.self_update)
        if changes.dry_run or outcomes[0].startswith("skip:"):
            for item in outcomes:
                say(item)
            return "already done" if not changes.dry_run else "previewed"
        for item in outcomes[:-1]:
            say(item)
        changes.summary = outcomes[-1]
        if "scheduled" in outcomes[-1]:
            return f"replacement scheduled. See {home / _uv_tool.HANDOVER_LOG}."
        if "not on PATH" in outcomes[-1]:
            return "not on PATH"
        return "on PATH" if outcomes[-1].endswith("is on PATH.") else f"installed ({version})"

    return Step(line, "legion command", home, run, needs_yes=needed)


def _account_request(home: Path, tool: str, source: str | None, account_id: str | None) -> tuple[Agent, Path | None]:
    settings = _settings_io.load_or_empty(home)
    given = None
    if source is not None:
        given = full_path(home_path(source, home))
        if not source or not given.is_dir():
            raise ValueError("Existing account config folder does not exist.")
        if account_id is None:
            account_id = next(
                (
                    agent.id
                    for agent in settings.agents
                    if agent.tool == tool and agent.provider is None and same_path(resolve_directory(_targets.account_folder(home, agent)), resolve_directory(given))
                ),
                None,
            )
    if account_id is None:
        number = 1
        while any(agent.id == f"{tool}{number}" for agent in settings.agents) or _launchers.find_command(f"{tool}{number}") is not None:
            number += 1
        account_id = f"{tool}{number}"
    existing = next((agent for agent in settings.agents if agent.id == account_id), None)
    if existing is not None:
        if existing.tool != tool or existing.provider is not None:
            raise ValueError(f"account '{account_id}' already exists with other values")
        if given is not None and not same_path(resolve_directory(_targets.account_folder(home, existing)), resolve_directory(given)):
            raise ValueError(f"account '{account_id}' already has its folder; legion add {tool} <folder> copies a folder into a new account")
        return existing, None
    _accounts.check_new_id(settings, account_id)
    folder = home / ".legion" / "agents" / account_id
    if given is not None and folder.is_dir() and any(folder.iterdir()):
        raise ValueError(f"{folder} already has files; adopt copies a folder into an empty one")
    return Agent.model_construct(id=account_id, tool=tool, config_dir=str(folder)), given


def _initialize_settings(home: Path, say: Say) -> str:
    with _settings_io.writer(home, require_existing=False):
        if settings_path(home).is_file():
            say("skip: settings (already done)")
            return "already done"
        _settings_io.save_locked(home, _settings_io.load_or_empty(home))
    return "created"


def _initialize_notes(home: Path, notes: Path, say: Say) -> str:
    with _settings_io.writer(home):
        if notes.is_file():
            say("skip: notes (already done)")
            return "already done"
        _generated.write_generated(notes, NOTES_TEMPLATE.read_text(encoding="utf-8-sig"), executable=False)
    return "created"


def _permission_change(folder: Path, rules: list[str]) -> dict[str, object] | None:
    file = folder / "settings.json"
    data = json.loads(read_text(file)) if file.exists() else {}
    if not isinstance(data, dict):
        raise TypeError(f"cannot read {file}: not a JSON object")
    permissions = data.get("permissions")
    if permissions is None:
        permissions = data["permissions"] = {}
    if not isinstance(permissions, dict):
        raise TypeError(f"cannot read {file}: permissions is not an object")
    current = permissions.get("allow")
    if current is None:
        current = []
    if isinstance(current, str):
        current = [current]
    if not isinstance(current, list):
        raise TypeError(f"cannot read {file}: permissions.allow is not a list or a rule")
    missing = [rule for rule in rules if rule not in current]
    if not missing:
        return None
    permissions["allow"] = [*[rule for rule in current if rule], *missing]
    return data


def _needs_rules(folder: Path, rules: list[str]) -> bool:
    if not rules:
        return False
    try:
        return _permission_change(folder, rules) is not None
    except (OSError, ValueError, TypeError):
        return True  # Report the error for this account when applying the plan.


def _install_cli(home: Path, entry: CatalogTool, say: Say) -> str:
    installer = _tool_install.ToolInstaller(home, _tool_install.current_platform(), True, say)
    if installer.install(entry.id) != 0:
        raise ValueError(f"{entry.id} install failed")
    _add_saved_folders()
    if _targets.account_command(home, entry, None) is None:
        raise ValueError(f"open a new terminal, then repeat setup for {entry.id}")
    return "available"


def _setup_steps(
    changes: Plan,
    home: Path,
    entries: list[CatalogTool],
    agents: list[Agent],
    source: str | Path,
    roots: list[Copy],
    requested: Agent | None,
    given: Path | None,
) -> bool:
    file = settings_path(home)
    notes = file.with_name("notes.md")
    if not file.is_file():
        changes.steps.append(Step(f"Create settings in {file}", "settings", file.parent, partial(_initialize_settings, home)))
    if not notes.is_file():
        changes.steps.append(Step(f"Create notes in {notes}", "notes", file.parent, partial(_initialize_notes, home, notes), needs_yes=False))

    by_tool = {entry.id: entry for entry in entries}
    missing_tools: set[str] = set()
    pending_plugin = False
    seen: set[tuple[str, str]] = set()
    for agent in agents:
        entry = by_tool[agent.tool]
        folder = _targets.account_folder(home, agent)
        key = agent.tool, _targets.folder_key(folder)
        requested_here = agent is requested
        if key in seen and not requested_here:
            continue
        seen.add(key)
        command = _targets.account_command(home, entry, agent.command)
        if command is None and agent.command is None and entry.id not in missing_tools:
            missing_tools.add(entry.id)
            hints = ", ".join(_tool_install.install_hints(entry.id, _tool_install.current_platform()))
            changes.steps.append(Step(f"Install {entry.id} ({hints})", entry.id, home, partial(_install_cli, home, entry)))
        origin = given if requested_here else None
        copy = _targets.plugin_copy(origin or folder, entry)
        rules = _accounts.get_tool(agent.tool).allow_rules or []
        needs_rules = _needs_rules(origin or folder, rules)
        if copy and command is not None and not requested_here:
            roots.append(copy)
            if not needs_rules:
                continue
        pending_plugin |= copy is None or requested_here
        actions = []
        if requested_here:
            actions.append(f"Add {agent.id} ({entry.id}) in {folder}" + (f" by copying {origin}" if origin else ""))
        if copy is None:
            actions.append(f"Install legion for {entry.id} in {folder}")
        if needs_rules:
            actions.append(f"Allow {', '.join(rules)} in {folder / 'settings.json'}")
        if not actions:
            actions.append(f"Check the command for {agent.id} in {folder}")
        action = partial(_setup_account, home, agent, entry, origin, requested_here, rules, source, roots)
        changes.steps.append(Step(" and ".join(actions), agent.id, folder, action))
    return pending_plugin


def _setup_account(
    home: Path,
    agent: Agent,
    entry: CatalogTool,
    origin: Path | None,
    requested: bool,
    rules: list[str],
    source: str | Path,
    roots: list[Copy],
    say: Say,
) -> str:
    folder = _targets.account_folder(home, agent)
    command = _targets.account_command(home, entry, agent.command)
    if command is None:
        raise ValueError(f"{entry.id} command is missing; run legion setup once it is available")
    guidance = ""
    if requested:
        if _uv_tool.find_uv() is None:
            raise ValueError(_uv_tool.UV_MISSING)
        for line in _accounts.add_subscription(home, agent.id, agent.tool, None, str(origin) if origin else None, say):
            say(line)
            if line.startswith("Log in once:"):
                guidance = " | log in:" + line.removeprefix("Log in once:")
    if rules:
        data = _permission_change(folder, rules)
        if data is not None:
            _generated.write_generated(folder / "settings.json", json.dumps(data, indent=2, ensure_ascii=False) + "\n", executable=False)
        else:
            say(f"skip: permission rules in {folder} (already done)")
    copy = _targets.plugin_copy(folder, entry)
    if copy:
        roots.append(copy)
        say(f"skip: legion in {folder} (already done)")
        return f"already done{guidance}"
    entry = entry.model_copy(update={"command": command})
    checkout = resolve_directory(Path(source)) if Path(source).is_dir() else None
    digest = _targets.plugin_digest(checkout / "plugins/legion") if checkout else None
    before, arguments = _plugins.installation_steps(entry, folder, source, "release", checkout=checkout, checkout_digest=digest)
    return _install_in(entry, folder, before, arguments, roots, say, checkout_digest=digest) + guidance


def _set_knowledge(home: Path, catalog: ToolsCatalog, choice: bool, say: Say) -> str:
    with _settings_io.writer(home, require_existing=False):
        settings = _settings_io.load_or_empty(home)
        if settings.knowledge_enabled == choice:
            say(f"skip: knowledge {'enabled' if choice else 'declined'} (already done)")
            return "already done"
        if not choice and settings.knowledge_enabled:
            for entry, folder, command, pin in _removal_targets(home, catalog, settings):
                if pin is None:
                    continue
                if command is None:
                    say(f"skip: knowledge: {entry.id} in {folder} (no command for this folder)")
                    continue
                target = entry.model_copy(update={"command": command})
                _remove_from(target, folder, _plugins.removal_steps(target, folder, pin), False, say)
        settings.knowledge_enabled = choice
        _settings_io.save_locked(home, settings)
    return "enabled" if choice else "declined"


def _with_knowledge(home: Path, run: Callable[[], str], say: Say) -> str:
    with _settings_io.writer(home, require_existing=False):
        if not _settings_io.load_or_empty(home).knowledge_enabled:
            say("skip: knowledge (off)")
            return "off"
        return run()


def _knowledge_steps(changes: Plan, home: Path, entries: list[CatalogTool], agents: list[Agent], pin: PluginPin | None, enabled: bool | None) -> None:
    if not enabled or pin is None:
        return

    def hub(say: Say) -> str:
        def initialize() -> str:
            lines: list[str] = []
            try:
                return "ready" if _knowledge_hub.initialize(home, lines) else "already done"
            finally:
                for line in lines:
                    say(line)

        return _with_knowledge(home, initialize, say)

    folder = home / ".legion/knowledge"
    if not changes.dry_run:
        changes.steps.append(Step(f"Prepare the knowledge hub in {folder}", "knowledge hub", folder, hub, needs_yes=False))

    def wiki(agent: Agent, entry: CatalogTool, folder: Path, say: Say) -> str:
        def ensure() -> str:
            command = _targets.account_command(home, entry, agent.command)
            if command is None:
                raise ValueError(f"{entry.id} command is missing")
            target = entry.model_copy(update={"command": command})
            return _plugins.ensure(target, folder, pin.source, pin.tag, changes.dry_run, say, pin=pin)

        if changes.dry_run:
            return ensure()
        return _with_knowledge(home, ensure, say)

    by_tool = {entry.id: entry for entry in entries}
    seen: set[tuple[str, str]] = set()
    for agent in agents:
        entry = by_tool[agent.tool]
        folder = _targets.account_folder(home, agent)
        key = agent.tool, _targets.folder_key(folder)
        if key not in seen and not _targets.is_default_folder(home, entry, folder):
            seen.add(key)
            changes.steps.append(Step(f"Ensure llm-wiki for {agent.id} in {folder}", f"{agent.id} knowledge", folder, partial(wiki, agent, entry, folder)))


def first_run(home: Path) -> None:
    stamp = home / ".legion/version"
    if not run_folder(home).is_dir() and not stamp.exists():
        return
    try:
        version = _uv_tool.plugin_version(PLUGIN_ROOT)
        if not owner_path_safe(stamp, home):
            raise ValueError(f"version stamp path contains a link: {stamp}")
        if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == version:
            return
        with saved_path():
            changes = plan(home, "finish")
            if apply(changes) == 0:
                _generated.write_generated(stamp, version + "\n", executable=False)
    except ERRORS as error:
        click.echo(f"First-run check: {' '.join(str(error).split())}", err=True)
