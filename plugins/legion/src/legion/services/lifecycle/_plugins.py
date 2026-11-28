# Apply and inspect plugin registrations using the tools catalog.

import json
import os
import re
import shutil
import tempfile
import tomllib
from collections import deque
from collections.abc import Callable
from pathlib import Path

from legion.services.accounts._launchers import command_prefix
from legion.services.lifecycle import _targets
from legion.services.lifecycle._targets import LEGION_MARKETPLACE, LEGION_PLUGIN
from legion.core._home import resolve_directory, same_path
from legion.core._run import display_command, run_command
from legion.core._settings_models import (
    CatalogTool,
    JsonListing,
    Plugin,
    PluginPin,
)


def _execute(entry: CatalogTool, arguments: list[str], folder: Path, listing: bool = False, progress: Callable[[str], None] | None = None) -> str:
    env = dict(os.environ)
    env[entry.config_env] = str(folder)
    if listing:
        env.update(entry.plugin.listing_env or {})
    command = entry.command if Path(entry.command).is_absolute() else shutil.which(entry.command) or entry.command
    program, prefix = command_prefix(Path(command))
    result = run_command(str(program), [*prefix, *arguments], env=env, cwd=Path(tempfile.gettempdir()), progress=progress)
    if result.exit_code:
        detail = result.error.strip() or result.output.strip()
        raise ValueError(f"{entry.command} {'listing ' if listing else ''}exited {result.exit_code}" + (f"\n{detail}" if detail else ""))
    return result.output


def _listing(entry: CatalogTool, folder: Path, kind: str) -> str:
    if not folder.is_dir():
        return ""
    arguments = entry.plugin.marketplace_list if kind == "marketplace" else entry.plugin.list_command
    return _execute(entry, arguments, folder, listing=True)


def _marketplace_item(entry: CatalogTool, folder: Path, name: str) -> dict[str, object]:
    output = _listing(entry, folder, "marketplace")
    if not output:
        return {}
    try:
        data = json.loads(output)
        listing = entry.plugin.marketplace_listing
        if listing.collection_field is not None:
            data = data[listing.collection_field]
        for item in data:
            if isinstance(item, dict) and item.get(listing.name_field) == name:
                return item
        return {}
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f"{entry.id} marketplace listing returned invalid JSON") from error


# The id is the first word of a listing row, or the second after a bullet in any spelling (the Windows console may print it as other
# characters). A continuation line such as "from <path>" has none. tests/integration/run.py keeps an identical copy.
def _row_id(line: str) -> str | None:
    words = line.split()
    if words and "@" in words[0]:
        return words[0]
    if len(words) > 1 and "@" in words[1] and words[0].rstrip(":") != "from":
        return words[1]
    return None


def _plugin_state(entry: CatalogTool, folder: Path, plugin_id: str) -> tuple[bool, bool, str | None]:
    output = _listing(entry, folder, "plugin")
    if not output:
        return False, False, None
    listing = entry.plugin.listing
    if isinstance(listing, JsonListing):
        try:
            items = json.loads(output)
            if not isinstance(items, list):
                raise TypeError("expected a JSON array")
        except (ValueError, TypeError) as error:
            raise ValueError(f"{entry.id} plugin listing returned invalid JSON") from error
        matches = [item for item in items if isinstance(item, dict) and item.get(listing.id_field) == plugin_id and isinstance(item.get(listing.enabled_field), bool)]
        enabled = [item for item in matches if item[listing.enabled_field]]
        version = next((item["version"] for item in enabled or matches if isinstance(item.get("version"), str)), None)
        return bool(matches), bool(enabled), version
    lines = [line for line in output.splitlines() if _row_id(line) == plugin_id and "not installed" not in line]
    enabled = [line for line in lines if not re.search(listing.reject_pattern, line)]
    version = None
    for line in enabled or lines:
        match = re.search(r"\s\(?v?(\d+\.\d+\.\d+(?:[-+][\w.+-]+)?)(?:\)|\s|$)", line)
        if match:
            version = match[1]
            break
    return bool(lines), bool(enabled), version


def verified_copy(entry: CatalogTool, folder: Path) -> tuple[str, Path] | None:
    _, enabled, version = _plugin_state(entry, folder, LEGION_PLUGIN)
    return _targets.plugin_copy(folder, entry, version=version) if enabled else None


def command_line(entry: CatalogTool, arguments: list[str], folder: Path) -> str:
    program, prefix = command_prefix(Path(entry.command))
    return f"{entry.config_env}={display_command([str(folder)])} {display_command([str(program), *prefix, *arguments])}"


def run_step(entry: CatalogTool, arguments: list[str], folder: Path, dry_run: bool, say: Callable[[str], None]) -> None:
    say(command_line(entry, arguments, folder))
    if dry_run:
        return
    tail: deque[str] = deque(maxlen=5)

    def progress(line: str) -> None:
        tail.append(line)
        say(line)

    try:
        _execute(entry, arguments, folder, progress=progress)
    except ValueError as error:
        detail = "\n".join(tail).strip()
        raise ValueError(f"{error}\n{detail}" if detail else str(error)) from error


def local_marketplace(item: dict[str, object]) -> Path | None:
    nested = item.get("marketplaceSource") or item.get("source")
    if isinstance(nested, dict):
        value = nested.get("path") or nested.get("source")
    elif nested == "directory":
        value = item.get("path")
    elif isinstance(nested, str):
        value = nested.removeprefix("Local: ").removeprefix("Directory: ")
    else:
        value = item.get("root")
    return resolve_directory(Path(value)) if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_dir() else None


def installation_steps(
    entry: CatalogTool,
    folder: Path,
    source: str | Path,
    ref: str,
    *,
    pin: PluginPin | None = None,
    updating: bool = False,
    checkout: Path | None = None,
    checkout_digest: str | None = None,
    source_explicit: bool = False,
) -> tuple[tuple[str, Path] | None, list[list[str]]]:
    plugin = _wiki_recipe(entry.plugin, pin) if pin else entry.plugin
    marketplace = pin.marketplace if pin else LEGION_MARKETPLACE
    plugin_id = pin.plugin_id if pin else LEGION_PLUGIN
    branch = pin.tag if pin else "main" if ref == "main" else "stable"
    template = plugin.marketplace_add if checkout else plugin.marketplace_add_ref
    location = checkout.as_posix() if checkout else str(source)
    add = [word.replace("{source}", location).replace("{ref}", branch) for word in template]

    item = _marketplace_item(entry, folder, marketplace)
    listed, enabled, version = _plugin_state(entry, folder, plugin_id)
    registered = bool(item)
    steps: list[list[str]] = []
    before = _targets.plugin_copy(folder, entry, version=version) if listed and not pin else None
    field = plugin.marketplace_listing.root_field
    root = item.get(field) if field else None
    ready = before is not None and registered and (field is None or (isinstance(root, str) and bool(root) and Path(root).is_dir()))
    local = local_marketplace(item)
    if pin:
        ready = registered and _pinned_marketplace(entry, folder, item, pin)
        if not ready:
            steps.extend(_removal_steps(plugin, listed, registered))
            registered = False
    elif checkout:
        matching = local is not None and same_path(checkout, local)
        if matching and ready and before and _targets.plugin_digest(before[1]) == checkout_digest:
            return before, [] if enabled else [plugin.install]
        steps.extend(_removal_steps(plugin, listed, registered))
        ready = registered = False
    elif (source_explicit or ref == "main") and registered and not _marketplace_matches(entry, folder, item, str(source), branch, marketplace):
        steps.extend(_removal_steps(plugin, listed, registered))
        ready = registered = False

    if ready and enabled and updating:
        if local and before and _targets.plugin_digest(local / "plugins/legion") == _targets.plugin_digest(before[1]):
            return before, []
        return before, [args for args in (plugin.marketplace_update, plugin.update) if args]
    if ready and enabled:
        return before, []
    if not registered:
        steps.append(add)
    if plugin.marketplace_update and not checkout:
        steps.append(plugin.marketplace_update)
    steps.append(plugin.install)
    return before, steps


def ensure(
    entry: CatalogTool,
    folder: Path,
    source: str | Path,
    ref: str,
    dry_run: bool,
    say: Callable[[str], None],
    *,
    pin: PluginPin,
) -> str:
    _, steps = installation_steps(entry, folder, source, ref, pin=pin)
    if not steps:
        say(f"skip: {pin.plugin_id} in {folder} (already done)")
        return "already done"
    if not dry_run:
        folder.mkdir(parents=True, exist_ok=True)
    for arguments in steps:
        run_step(entry, arguments, folder, dry_run, say)
    if not dry_run and not _pinned_marketplace(entry, folder, _marketplace_item(entry, folder, pin.marketplace), pin):
        raise ValueError(f"llm-wiki marketplace could not be verified at {pin.tag}")
    return "previewed" if dry_run else "ready"


def _wiki_recipe(plugin: Plugin, pin: PluginPin) -> Plugin:
    names = {LEGION_MARKETPLACE: pin.marketplace, LEGION_PLUGIN: pin.plugin_id}
    return plugin.model_copy(
        update={
            "install": [names.get(word, word) for word in plugin.install],
            "uninstall": [names.get(word, word) for word in plugin.uninstall],
            "marketplace_remove": [names.get(word, word) for word in plugin.marketplace_remove],
            "marketplace_update": [],
        }
    )


def _pinned_marketplace(entry: CatalogTool, folder: Path, item: dict[str, object], pin: PluginPin) -> bool:
    return _marketplace_matches(entry, folder, item, pin.source, pin.tag, pin.marketplace)


def _marketplace_matches(entry: CatalogTool, folder: Path, item: dict[str, object], expected_source: str, tag: str, marketplace: str) -> bool:
    nested = item.get("marketplaceSource") or item.get("source")
    details = nested if isinstance(nested, dict) else item
    source = details.get("repo") or details.get("url") or details.get("source")
    if not isinstance(source, str):
        return False
    source, _, source_ref = source.partition("#")
    if source.removeprefix("GitHub: ").removesuffix(".git").removeprefix("https://github.com/").removeprefix("git@github.com:") != expected_source:
        return False
    ref = details.get("ref") or item.get("ref") or source_ref
    if not ref:
        # Codex and Copilot can omit the ref from their marketplace listings.
        try:
            if entry.id == "codex":
                ref = tomllib.loads((folder / "config.toml").read_text(encoding="utf-8-sig"))["marketplaces"][marketplace]["ref"]
            elif entry.id == "copilot":
                ref = json.loads((folder / "settings.json").read_text(encoding="utf-8-sig"))["extraKnownMarketplaces"][marketplace]["source"]["ref"]
        except (OSError, ValueError, TypeError, KeyError):
            return False
    return ref == tag


def _removal_steps(plugin: Plugin, listed: bool, registered: bool) -> list[list[str]]:
    return [arguments for present, arguments in ((listed, plugin.uninstall), (registered, plugin.marketplace_remove)) if present]


def removal_steps(entry: CatalogTool, folder: Path, pin: PluginPin | None = None) -> list[list[str]]:
    plugin = _wiki_recipe(entry.plugin, pin) if pin else entry.plugin
    listed, _, _ = _plugin_state(entry, folder, pin.plugin_id if pin else LEGION_PLUGIN)
    registered = bool(_marketplace_item(entry, folder, pin.marketplace if pin else LEGION_MARKETPLACE))
    return _removal_steps(plugin, listed, registered)
