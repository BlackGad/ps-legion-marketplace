# Read Legion's CLI recipes from the selected marketplace source.

import re
from pathlib import Path

import httpx
from pydantic import ValidationError

from legion.core._http import HttpsError, failure_text, get_bytes
from legion.core._plugin import PLUGIN_ROOT
from legion.core._settings_models import CatalogTool, ToolsCatalog

DEFAULT_SOURCE = "BlackGad/ps-legion-marketplace"


def load_catalog(source: str | Path, ref: str) -> ToolsCatalog:
    if Path(source).is_dir():
        data = (Path(source) / "plugins/legion/tools.json").read_bytes()
    elif str(source) == DEFAULT_SOURCE and ref != "main":
        data = (PLUGIN_ROOT / "tools.json").read_bytes()
    elif re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(source)):
        branch = "main" if ref == "main" else "stable"
        url = f"https://raw.githubusercontent.com/{source}/{branch}/plugins/legion/tools.json"
        try:
            data = get_bytes(url, 30)
        except (HttpsError, httpx.HTTPError) as error:
            raise ValueError(f"could not read the tools catalog {url}: {failure_text(error)}") from error
    else:
        raise ValueError(f"unsupported marketplace source '{source}'")
    try:
        return ToolsCatalog.model_validate_json(data)
    except ValidationError as error:
        raise ValueError(f"invalid tools catalog: {error}") from error


def catalog_tool(catalog: ToolsCatalog, tool_id: str) -> CatalogTool:
    for entry in catalog.tools:
        if entry.id == tool_id:
            return entry
    raise ValueError(f"unknown tool '{tool_id}'")
