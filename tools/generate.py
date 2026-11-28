# From the repository root: uv run --project plugins/legion python tools/generate.py

import json
from pathlib import Path
from typing import Optional

import click
from pydantic_core import from_json

from legion.core import _schemas
from legion.core._atomic_write import atomic_write_bytes
from legion.core._settings_models import (
    ChannelDescriptor,
    HelpersCatalog,
    ToolDescriptor,
    ToolsCatalog,
)
from legion.core._validate import read_text

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "legion"
WIKI = {
    "marketplace": "llm-wiki",
    "pluginId": "wiki@llm-wiki",
    "source": "nvk/llm-wiki",
    "tag": "v1.0.0",
    "commit": "35d37c5591c7585643196333edd83922e7bbc757",
}


def _check_skill(path: Path, kind: str, skill_id: str) -> None:
    if path.parent.name != f"legion-{skill_id}":
        raise ValueError(f"{kind} id '{skill_id}' does not match its skill directory: {path}")
    if not (path.parent / "SKILL.md").is_file():
        raise ValueError(f"{kind} '{skill_id}' requires SKILL.md: {path}")


def _check_channels(skills: Path, tool_ids: set[str]) -> None:
    for path in _descriptor_paths(skills, "channel.json"):
        try:
            channel = ChannelDescriptor.model_validate_json(read_text(path))
        except ValueError as error:
            raise ValueError(f"invalid channel descriptor '{path}': {error}") from error
        _check_skill(path, "channel", channel.id)
        for tool_id in channel.launch:
            if tool_id not in tool_ids:
                raise ValueError(f"unknown channel tool '{tool_id}': {path}")


def _check_helpers(path: Path, tools: dict[str, ToolDescriptor]) -> None:
    try:
        helpers = HelpersCatalog.model_validate_json(read_text(path)).root
    except ValueError as error:
        raise ValueError(f"invalid helper recipes '{path}': {error}") from error
    for helper_id in helpers:
        if helper_id in tools:
            raise ValueError(f"helper/tool id collision '{helper_id}': {path}")
    recipes = {**helpers, **{tool.id: tool.install for tool in tools.values()}}
    for recipe_id, recipe in recipes.items():
        for platform in (recipe.macos, recipe.linux):
            for need in platform.needs:
                if need not in helpers:
                    raise ValueError(f"unknown helper prerequisite '{need}' for '{recipe_id}'")
            if platform.needs and recipe_id in helpers:
                raise ValueError(f"helper '{recipe_id}' cannot have prerequisites")


def _descriptor_paths(skills: Path, file_name: str) -> list[Path]:
    return sorted((path for path in skills.glob(f"legion-*/{file_name}") if path.is_file()), key=lambda path: path.parent.name)


def render_tools_index(skills: Path) -> str:
    if not skills.is_dir():
        raise ValueError(f"missing skills directory: {skills}")
    tools: dict[str, ToolDescriptor] = {}
    entries: dict[str, dict[str, object]] = {}
    skill_error: Optional[ValueError] = None
    for path in _descriptor_paths(skills, "tool.json"):
        try:
            source = from_json(read_text(path))
            tool = ToolDescriptor.model_validate(source)
        except ValueError as error:
            raise ValueError(f"invalid tool descriptor '{path}': {error}") from error
        if tool.id in tools:
            raise ValueError(f"duplicate tool id '{tool.id}': {path}")
        tools[tool.id] = tool
        entries[tool.id] = {
            "id": tool.id,
            "command": tool.command,
            "configEnv": tool.config_env,
            # 0.23.1 reads this catalog when given a checkout. Adoption reads the descriptor.
            "plugin": {key: value for key, value in source["plugin"].items() if key != "adoptRewrite"},
            "hooks": source["hooks"],
            "installHint": tool.install.hint,
        }
        # Descriptor errors (including duplicate IDs) take precedence over skill layout errors.
        if skill_error is None:
            try:
                _check_skill(path, "tool", tool.id)
            except ValueError as error:
                skill_error = error
    if skill_error is not None:
        raise skill_error
    _check_channels(skills, set(tools))
    _check_helpers(skills.parent / "helpers.json", tools)

    data = {"schemaVersion": 1, "wiki": WIKI, "tools": [entries[tool_id] for tool_id in sorted(entries)]}
    ToolsCatalog.model_validate(data)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


@click.command()
@click.option("--check", is_flag=True, help="Check the tools index without writing files.")
def main(check: bool) -> None:
    try:
        content = render_tools_index(PLUGIN_ROOT / "skills").encode("utf-8")
        index = PLUGIN_ROOT / "tools.json"
        current = index.is_file() and index.read_bytes() == content
        if check and not current:
            raise click.ClickException("tools index is missing or out of date. Run tools/generate.py.")
        if not check:
            _schemas.main(PLUGIN_ROOT / "schemas")
            if not current:
                atomic_write_bytes(index, content, keep_mode=False, add_mode=0o644)
        click.echo("skip: tools index (already done)" if current else "updated: tools index")
    except (OSError, ValueError) as error:
        raise click.ClickException(str(error)) from error


if __name__ == "__main__":
    main()
