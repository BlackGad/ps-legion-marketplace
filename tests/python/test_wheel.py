import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from conftest import REPO

PLUGIN = REPO / "plugins/legion"


@pytest.fixture(scope="session")
def wheel_uv() -> list[str]:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the wheel smoke test"
    # Discover the cache populated by uv run before the per-test fence sets UV_CACHE_DIR.
    cache = subprocess.check_output([uv, "cache", "dir", "--offline"], encoding="utf-8", timeout=30).strip()
    return [uv, "--offline", "--no-python-downloads", "--cache-dir", cache]


@pytest.mark.wheel
def test_wheel_runs_without_its_plugin_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wheel_uv: list[str]) -> None:
    version = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]
    monkeypatch.setenv("PYTHONPATH", str(REPO / "tests/python/socket_fence"))
    monkeypatch.setenv("UV_LINK_MODE", "copy")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    dist = tmp_path / "dist"
    venv = tmp_path / "venv"
    scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    legion = scripts / ("legion.exe" if sys.platform == "win32" else "legion")
    with TemporaryDirectory(dir=tmp_path) as staging:
        source = Path(staging) / "plugin"
        shutil.copytree(PLUGIN, source, ignore=shutil.ignore_patterns("__pycache__", ".venv", "dist"))
        build = subprocess.run(
            [*wheel_uv, "build", "--python", sys.executable, "--out-dir", str(dist), str(source)],
            cwd=tmp_path,
            capture_output=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        if build.returncode and "resolve requirements from `build-system.requires`" in build.stderr and "not found in the cache" in build.stderr:
            pytest.skip("uv cache lacks the wheel build backend or one of its dependencies")
        assert build.returncode == 0, build.stderr
        (wheel,) = dist.glob("*.whl")
        assert wheel.name == f"legion-{version}-py3-none-any.whl"

        # Populate dependencies from the same lock in a fresh environment, then install the built wheel.
        monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(venv))
        subprocess.run([*wheel_uv, "sync", "--frozen", "--no-default-groups", "--extra", "mcp", "--no-install-project", "--python", sys.executable], cwd=source, check=True, timeout=120)
        subprocess.run([*wheel_uv, "pip", "install", "--no-deps", "--python", str(python), str(wheel)], cwd=tmp_path, check=True, timeout=120)

    assert not source.exists()
    result = subprocess.check_output([str(legion), "--version"], cwd=tmp_path, text=True, timeout=30)
    assert result.strip() == f"legion {version}"

    probe = """
from importlib.metadata import version
from pathlib import Path
import sys
import legion
from legion.services.lifecycle._lifecycle import NOTES_TEMPLATE
from legion.services.installation._tool_install import find_recipe, load_tools
from legion.services.lifecycle._catalog import DEFAULT_SOURCE, load_catalog
from legion.services.repository._repository import _check_templates

assert Path(legion.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert version("legion") == sys.argv[1]
assert {tool.id for tool in load_catalog(DEFAULT_SOURCE, "release").tools} == {"claude", "codex", "copilot"}
assert {tool.id for tool in load_tools()} == {"claude", "codex", "copilot"}
assert find_recipe("uv").check == ["uv", "--version"]
assert NOTES_TEMPLATE.read_text(encoding="utf-8").strip()
_check_templates()
import mcp.server
"""
    subprocess.run([str(python), "-c", probe, version], cwd=tmp_path, check=True, timeout=30)
