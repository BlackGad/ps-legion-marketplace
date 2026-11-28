import ast
from pathlib import Path

from legion import cli


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "plugins/legion/src/legion"


def test_runtime_modules_live_in_packages() -> None:
    assert sorted(path.name for path in PACKAGE.glob("_*.py")) == ["__init__.py"]
    assert (PACKAGE / "cli/__init__.py").is_file()
    assert (PACKAGE / "hook/__init__.py").is_file()
    assert (PACKAGE / "services/lifecycle/_lifecycle.py").is_file()
    assert not (PACKAGE / "services/installation/installer/__init__.py").exists()
    assert (PACKAGE / "services/installation/_uv_tool.py").is_file()
    assert (PACKAGE / "core/_stop_event.py").is_file()


def test_the_hidden_refresh_command_is_gone() -> None:
    assert "refresh" not in cli.main.commands


def test_services_are_grouped_by_area() -> None:
    services = PACKAGE / "services"
    assert sorted(path.name for path in services.glob("*.py")) == ["__init__.py"]
    for area in ("accounts", "installation", "lifecycle", "repository", "channels", "foundry", "limits", "status"):
        assert (services / area / "__init__.py").is_file()


def test_core_and_services_import_only_lower_layers() -> None:
    for layer, forbidden in (("core", ("legion.services", "legion.cli", "legion.hook")), ("services", ("legion.cli", "legion.hook"))):
        for source in (PACKAGE / layer).rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imported = [f"{node.module}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module for alias in node.names]
            imported += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            imported += [
                node.args[0].value
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ]
            assert not any(name == prefix or name.startswith(prefix + ".") for name in imported for prefix in forbidden), source


def test_console_target_uses_cli_package() -> None:
    project = (ROOT / "plugins/legion/pyproject.toml").read_text(encoding="utf-8")
    assert 'legion = "legion.cli:main"' in project


def test_only_bootstrap_repository_checks_use_powershell() -> None:
    assert not list((ROOT / "tests").glob("*.Tests.ps1"))
    assert [path.name for path in (ROOT / "tests/repository").glob("*.Tests.ps1")] == ["Bootstrap.Tests.ps1"]
