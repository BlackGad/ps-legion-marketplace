# Local, stateful stand-ins for the three plugin CLIs.

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "copilot": "COPILOT_HOME"}
MARKETPLACE = "ps-legion-marketplace"
PLUGIN = f"legion@{MARKETPLACE}"
FAKE_SESSION = "2df8daf6-4f6d-4dd8-8601-cc8a83fe07ff"


def write_shim(bin_dir: Path, name: str, script: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        (bin_dir / f"{name}.cmd").write_text(f'@echo off\n"{sys.executable}" "{script}" "%~n0" %*\n', encoding="utf-8")
    else:
        shim = bin_dir / name
        command = shlex.join([sys.executable, str(script)])
        shim.write_text(f'#!/bin/sh\nexec {command} "${{0##*/}}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)


def log_call(path: Path, args: list[str]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        print(json.dumps(args), file=stream)


def plugin_root(tool: str, folder: Path, plugin: str = PLUGIN) -> Path:
    name, market = plugin.split("@")
    if tool == "copilot":
        return folder / "installed-plugins" / market / name
    return folder / "plugins/cache" / market / name


def list_plugins(tool: str, folder: Path, installed: list[str], disabled: list[str]) -> None:
    rows = []
    for plugin in installed:
        root = plugin_root(tool, folder, plugin)
        version = json.loads((root / "plugin.json").read_text(encoding="utf-8"))["version"]
        enabled = plugin not in disabled
        if tool == "claude":
            rows.append({"id": plugin, "version": version, "installPath": str(root), "enabled": enabled})
        elif tool == "codex":
            print(f"{plugin} {version} {root}" + (" (disabled)" if not enabled else ""))
        else:
            print(f"  * {plugin} (v{version}) ({'enabled' if enabled else 'disabled'})")
    if tool == "claude":
        print(json.dumps(rows))


def overseer(tool: str, args: list[str], folder: Path, source: str) -> int:
    repo = Path.cwd()
    log_call(repo / "overseer.jsonl", args)
    mode = os.environ.get("FAKE_OVERSEER_UVX")
    legion = ["legion"] if mode else [sys.executable, "-m", "legion.cli"]
    resumed = args[0] in ("--resume", "resume") or args[0].startswith("--resume=")
    if tool == "claude" and not resumed:
        project = folder / "projects/fake"
        project.mkdir(parents=True, exist_ok=True)
        (project / f"{args[1]}.jsonl").touch()
    subprocess.run(
        [*legion, "--home", os.environ["LEGION_HOME"], "lease", "take", "--tool", tool, "--account", f"{tool}1", "--process-id", str(os.getpid())],
        check=True,
        env=os.environ | {"CODEX_THREAD_ID": FAKE_SESSION},
    )
    if resumed and not (repo / "request-again").exists():
        assert "Continue the fake task." in (repo / ".legion/state/handoff.md").read_text(encoding="utf-8")
        (repo / "resumed-config.json").write_text(json.dumps([str(folder), os.environ.get("TELEGRAM_STATE_DIR")]), encoding="utf-8")
        return 0
    if tool == "codex":
        # The test requests from outside this process tree, as an app-server command does.
        (repo / "codex-ready").touch()
        time.sleep(60)
        return 0
    (repo / "request-again").unlink(missing_ok=True)
    if mode == "1":
        legion = ["uvx", "--isolated", "--from", f"{Path(source) / 'plugins/legion'}[mcp]", "legion"]
    copilot = ["--session-id", FAKE_SESSION] if tool == "copilot" else []
    bare_python = os.environ.get("FAKE_OVERSEER_BARE_PYTHON")
    command = [bare_python, "-m", "legion.cli", "update", "--self", "--yes"] if bare_python else [*legion, "update", "--self", "--yes", "--source", source, *copilot]
    request = subprocess.run(command, check=False, capture_output=True, text=True)
    (repo / "self-update-request.txt").write_text(request.stdout + request.stderr, encoding="utf-8")
    if not bare_python:
        request.check_returncode()
    time.sleep(60)
    return 0


def main(tool: str, args: list[str]) -> int:
    folder = Path(os.environ[CONFIG[tool]])
    folder.mkdir(parents=True, exist_ok=True)
    log_call(folder / "fake-cli.log", args)
    if args[:1] != ["plugin"] and (source := os.environ.get("FAKE_OVERSEER_REQUEST")):
        return overseer(tool, args, folder, source)
    group, verb, *operands = args
    if group != "plugin":
        raise ValueError(f"unexpected {tool} arguments: {args}")
    marketplace = verb == "marketplace"
    if marketplace:
        verb, *operands = operands
    qualified = f"marketplace {verb}" if marketplace else f"plugin {verb}"
    fail_folder = os.environ.get("FAKE_CLI_FAIL_FOLDER")
    if os.environ.get("FAKE_CLI_FAIL") in (verb, qualified) and (fail_folder is None or Path(fail_folder) == folder):
        print(f"fake {tool}: {qualified} failed", file=sys.stderr)
        return 1

    state_file = folder / "fake-cli.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"marketplaces": {}, "plugins": []}
    markets: dict[str, str | dict[str, str]] = state["marketplaces"]
    installed: list[str] = state["plugins"]
    if verb == "list":
        assert operands == (["--json"] if marketplace or tool == "claude" else []), args
        if marketplace:
            rows = [{"name": name, **(root if isinstance(root, dict) else {"root": root})} for name, root in markets.items()]
            print(json.dumps({"marketplaces": rows} if tool == "codex" else rows))
        else:
            list_plugins(tool, folder, installed, state.get("disabled", []))
        return 0

    if marketplace and verb == "add":
        [location] = operands
        source = Path(location).resolve()
        manifest = json.loads((source / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        markets[manifest["name"]] = str(source)
    elif marketplace and verb == "remove":
        name, *flags = operands
        assert flags == (["--force"] if tool == "copilot" else []), args
        del markets[name]
    elif marketplace and verb == ("upgrade" if tool == "codex" else "update"):
        # Local sources are already current. Plugin update reads the latest files there.
        # Codex refuses to upgrade a local marketplace by name; a bare upgrade skips it.
        assert (tool == "codex" and not operands) or (tool != "codex" and len(operands) == 1 and operands[0] in markets), args
    elif not marketplace and verb in (("add",) if tool == "codex" else ("install", "update")):
        [plugin] = operands
        name, market = plugin.split("@")
        location = markets[market]
        assert isinstance(location, str), "fake installs require a local marketplace"
        source = Path(location)
        manifest = json.loads((source / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        relative = next(row["source"] for row in manifest["plugins"] if row["name"] == name)
        root = plugin_root(tool, folder, plugin)
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / relative, root, dirs_exist_ok=True)
        if plugin not in installed:
            installed.append(plugin)
        if plugin in state.get("disabled", []):
            state["disabled"].remove(plugin)
    elif not marketplace and verb == ("remove" if tool == "codex" else "uninstall"):
        [plugin] = operands
        installed.remove(plugin)
        shutil.rmtree(plugin_root(tool, folder, plugin))
    else:
        raise ValueError(f"unexpected {tool} arguments: {args}")
    state_file.write_text(json.dumps(state), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
