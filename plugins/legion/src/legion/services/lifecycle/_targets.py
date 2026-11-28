# Managed folders and the plugin copies on disk.

import hashlib
import json
import os
import shutil
from pathlib import Path

from legion.core._home import (
    full_path,
    home_path,
    is_under,
    resolve_directory,
    same_path,
)
from legion.core._settings_models import (
    Agent,
    CatalogTool,
    PluginManifest,
    ToolDescriptor,
)
from legion.core._validate import read_text
from legion.services.accounts._launchers import registered_command
from legion.services.installation._update import compare_versions

LEGION_MARKETPLACE = "ps-legion-marketplace"
LEGION_PLUGIN = f"legion@{LEGION_MARKETPLACE}"


def account_folder(home: Path, agent: Agent) -> Path:
    return full_path(home_path(agent.config_dir, home))


def account_command(home: Path, tool: CatalogTool | ToolDescriptor, configured: str | None) -> str | None:
    if configured is None:
        return tool.command if shutil.which(tool.command) else None
    command = registered_command(configured, home)
    return str(command) if command else None


def folder_key(folder: Path) -> str:
    return os.path.normcase(str(resolve_directory(folder)))


def default_folder(home: Path, tool: CatalogTool | ToolDescriptor) -> Path | None:
    value = tool.plugin.default_config_dir
    return full_path(home_path(value, home)) if value else None


def outlander(home: Path, tool: CatalogTool | ToolDescriptor) -> tuple[Path, str] | None:
    folder = default_folder(home, tool)
    return (folder, tool.command) if folder is not None and shutil.which(tool.command) else None


def folders(home: Path, tool: CatalogTool, agents: list[Agent]) -> list[tuple[Path, str | None]]:
    accounts = [(account_folder(home, agent), account_command(home, tool, agent.command)) for agent in agents if agent.tool == tool.id]
    targets: dict[str, tuple[Path, str | None]] = {}
    stock = outlander(home, tool)
    for folder, command in ([stock] if stock else []) + accounts:
        key = folder_key(folder)
        if key not in targets or targets[key][1] is None:
            targets[key] = folder, command
    return list(targets.values())


def _copilot_local_manifest(folder: Path) -> Path | None:
    try:
        settings = json.loads(read_text(folder / "settings.json"))
        source = settings["extraKnownMarketplaces"][LEGION_MARKETPLACE]["source"]
        if source["source"] != "directory" or not isinstance(settings["enabledPlugins"][LEGION_PLUGIN], bool):
            return None
        root = resolve_directory(Path(source["path"]))
        registry = json.loads(read_text(root / ".claude-plugin/marketplace.json"))
        if registry["name"] == LEGION_MARKETPLACE:
            for plugin in registry["plugins"]:
                if plugin["name"] == "legion" and isinstance(plugin["source"], str):
                    path = resolve_directory(root / plugin["source"])
                    if is_under(path, root, allow_equal=True):
                        return path / "plugin.json"
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def _claude_manifests(folder: Path, cache: Path) -> list[Path]:
    try:
        registration = json.loads(read_text(folder / "plugins/installed_plugins.json"))["plugins"][LEGION_PLUGIN]
        return [
            path / "plugin.json"
            for record in registration
            if record.get("scope", "user") == "user"
            and isinstance(record.get("installPath"), str)
            and (path := Path(record["installPath"])).is_absolute()
            and is_under(resolve_directory(path), resolve_directory(cache), allow_equal=True)
        ]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []


def plugin_copy(folder: Path, tool: CatalogTool | ToolDescriptor, name: str = "legion", *, version: str | None = None) -> tuple[str, Path] | None:
    root = tool.plugin.installed_root
    if name == "wiki":
        root = "installed-plugins/llm-wiki/wiki" if tool.id == "copilot" else "plugins/cache/llm-wiki/wiki"
    local = _copilot_local_manifest(folder) if name == "legion" and tool.id == "copilot" else None
    cache = folder / root if root else None
    files = [local] if local is not None else sorted(cache.rglob("plugin.json")) if cache else []
    if cache is not None and name == "legion" and tool.id == "claude":
        files = _claude_manifests(folder, cache) or files
    latest = None
    for file in files:
        if name == "legion" and file.parent.name in (".claude-plugin", ".codex-plugin"):
            continue
        try:
            manifest = PluginManifest.model_validate_json(read_text(file))
            if manifest.name != name or (version is not None and manifest.version != version):
                continue
            if compare_versions(manifest.version, latest[0] if latest else manifest.version) > 0 or latest is None:
                latest = manifest.version, file.parent
        except (OSError, ValueError):
            continue
    return latest


def installed_version(folder: Path, tool: CatalogTool | ToolDescriptor, name: str = "legion") -> str | None:
    copy = plugin_copy(folder, tool, name)
    return copy[0] if copy else None


def is_default_folder(home: Path, tool: CatalogTool | ToolDescriptor, folder: Path) -> bool:
    default = default_folder(home, tool)
    return default is not None and same_path(resolve_directory(default), resolve_directory(folder))


# Hash shipped plugin files without build and CLI cache metadata.
def plugin_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in (".claude-plugin", ".codex-plugin", ".mcp.json", "hatch_build.py", "helpers.json", "hooks", "plugin.json", "pyproject.toml", "schemas", "skills", "src", "tools.json", "uv.lock"):
        item = root / name
        files = sorted(item.rglob("*")) if item.is_dir() else [item]
        for file in files:
            relative = file.relative_to(root)
            if file.is_file() and "__pycache__" not in relative.parts and not any(part.endswith(".egg-info") for part in relative.parts):
                digest.update(relative.as_posix().encode())
                digest.update(b"\0")
                digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()
