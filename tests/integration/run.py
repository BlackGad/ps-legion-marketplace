"""
End-to-end checks against disposable homes and public Legion commands.

On Windows with uv and the three coding CLIs on PATH, set CI=true and PYTHONUTF8=1, then run
python tests/integration/run.py --home-root <new-temp-dir>/home --out <results>.
"""

import argparse
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import signal
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOLS = ("claude", "codex", "copilot")
SETUP_ACCOUNTS = {tool: f"ci-{tool}" for tool in TOOLS}
MARKETPLACE = "BlackGad/ps-legion-marketplace"
WIKI_MARKETPLACE = "llm-wiki"
WIKI_PLUGIN = "wiki@llm-wiki"
WIKI_SOURCE = "nvk/llm-wiki"
WIKI_TAG = "v1.0.0"
WIKI_COMMIT = "35d37c5591c7585643196333edd83922e7bbc757"
ENDPOINT = "https://example-resource.cognitiveservices.azure.com"
WINDOWS = sys.platform == "win32"
CODEX_PROBE_SECONDS = 120
INSTALLATION_NO_CHANGES = "Nothing to do. Legion is already installed for the available targets."
UPDATE_NO_CHANGES = "Nothing to do. Legion is already up to date."


@dataclass
class Case:
    name: str
    repeat: bool
    result: str
    seconds: float
    message: str
    log: str

    @property
    def title(self) -> str:
        return self.name + (" (repeat)" if self.repeat else "")


def codex_legion_server(servers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the connected legion server that lists the status tool, from a Codex mcpServerStatus/list reply."""
    return next((server for server in servers if server.get("name") == "legion" and server.get("runtimeStatus") == "connected" and "status" in server.get("tools", {})), None)


def stop_process_tree(process: "subprocess.Popen[str]") -> None:
    """Close stdin, give the process a moment to leave, then end its whole process group so an npm shim cannot leave the native app-server running."""
    if process.stdin:
        process.stdin.close()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    if sys.platform == "win32":
        subprocess.run([shutil.which("taskkill") or "taskkill", "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)  # noqa: S603
    else:
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            with suppress(ProcessLookupError, OSError):
                os.killpg(process.pid, signal_number)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and group_exists(process.pid):
                time.sleep(0.1)
    process.wait(timeout=5)


def group_exists(group: int) -> bool:
    if sys.platform == "win32":
        return False
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def probe_codex_mcp(command: list[str], env: dict[str, str], cwd: Path, seconds: float) -> list[dict[str, Any]]:
    """Ask a Codex app-server for legion's MCP status within one deadline; retry while legion is absent or starting."""
    deadline = time.monotonic() + seconds
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen(  # noqa: S603
        command, cwd=cwd, env=env, encoding="utf-8", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=sys.platform != "win32"
    )
    replies: queue.Queue[dict[str, Any] | Exception] = queue.Queue()

    def read_replies() -> None:
        try:
            for line in process.stdout or []:
                replies.put(json.loads(line))
            replies.put(EOFError("codex app-server closed its output"))
        except Exception as error:
            replies.put(error)

    threading.Thread(target=read_replies, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def call(number: int, method: str, params: dict[str, Any]) -> Any:
        send({"id": number, "method": method, "params": params})
        while True:
            try:
                reply = replies.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                raise AssertionError(f"codex app-server timed out in {method}") from None
            require(not isinstance(reply, Exception), f"codex app-server failed in {method}: {type(reply).__name__}: {reply}")
            if isinstance(reply, dict) and reply.get("id") == number:
                require("error" not in reply, f"codex app-server {method} failed: {reply.get('error')}")
                return reply["result"]

    servers: list[dict[str, Any]] = []
    try:
        call(1, "initialize", {"clientInfo": {"name": "legion-integration", "version": "1"}})
        send({"method": "initialized"})
        thread = call(2, "thread/start", {"cwd": str(cwd)})["thread"]["id"]
        number = 3
        while time.monotonic() < deadline:
            servers = call(number, "mcpServerStatus/list", {"threadId": thread, "serverName": "legion"})["data"]
            number += 1
            settled = any(server.get("name") == "legion" and server.get("runtimeStatus") not in (None, "notStarted", "starting") for server in servers)
            if settled:
                break
            time.sleep(1)
    finally:
        stop_process_tree(process)
    return servers


# The id is the first word of a listing row, or the second after a bullet in any spelling (the Windows console may print it as other
# characters). A continuation line such as "from <path>" has none. This is an identical copy of _row_id in the installer.
def _row_id(line: str) -> str | None:
    words = line.split()
    if words and "@" in words[0]:
        return words[0]
    if len(words) > 1 and "@" in words[1] and words[0].rstrip(":") != "from":
        return words[1]
    return None


def listed(entry: dict[str, Any], output: str, include_disabled: bool = False, kind: str = "plugin", *, wanted: str | None = None) -> bool:
    """Read CLI listings using the formats advertised by tools.json."""
    # Hand-built, reason: the three public CLIs expose different listing formats described by tools.json.
    if not output.strip():
        return False
    wanted = wanted or ("ps-legion-marketplace" if kind == "marketplace" else "legion@ps-legion-marketplace")
    listing = entry["plugin"]["marketplaceListing" if kind == "marketplace" else "listing"]
    if listing["format"] == "json":
        items = json.loads(output)
        if "collectionField" in listing:
            items = items[listing["collectionField"]]
        for item in items:
            if item.get(listing.get("nameField", listing.get("idField"))) == wanted:
                return include_disabled or kind == "marketplace" or item.get(listing["enabledField"]) is True
        return False
    matches = [line for line in output.splitlines() if _row_id(line) == wanted and "not installed" not in line]
    return bool(matches) if include_disabled else any(not re.search(listing["rejectPattern"], line) for line in matches)


def write_report(out: Path, system: str, cases: list[Case], probe: list[str], extra: str = "") -> None:
    """Write the JUnit, benchmark and Markdown artifacts consumed by CI."""
    # Hand-built, reason: the benchmark action needs the existing custom JSON shape alongside JUnit and a readable summary.
    out.mkdir(parents=True, exist_ok=True)
    suite = ET.Element(
        "testsuite",
        name=f"integration.{system}",
        tests=str(len(cases)),
        failures=str(sum(c.result == "failed" for c in cases)),
        skipped=str(sum(c.result == "skipped" for c in cases)),
        time=f"{sum(c.seconds for c in cases):.3f}",
    )
    for case in cases:
        element = ET.SubElement(suite, "testcase", classname=f"integration.{system}", name=case.title, time=f"{case.seconds:.3f}")
        if case.result != "passed":
            # Hand-built, reason: JUnit XML 1.0 cannot represent terminal control sequences.
            clean = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[0-~])", "", case.message)
            clean = re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd]", "", clean)
            ET.SubElement(element, "failure" if case.result == "failed" else "skipped", message=clean)
    ET.indent(suite)
    ET.ElementTree(suite).write(out / f"integration-{system}.xml", encoding="utf-8", xml_declaration=True)
    benchmark = []
    for case in cases:
        if case.result == "passed":
            entry: dict[str, Any] = {"name": f"{system} / {case.title}", "unit": "s", "value": case.seconds}
            if extra and not case.repeat:
                entry["extra"] = extra
            benchmark.append(entry)
    (out / f"integration-bench-{system}.json").write_text(json.dumps(benchmark, indent=2) + "\n", encoding="utf-8")
    lines = [f"## Integration ({system})", "", "### Probe", "", *(f"- {item}" for item in probe), "", "| step | first s | repeat s | result |", "| --- | --- | --- | --- |"]
    for first, repeat in zip(cases[::2], cases[1::2], strict=True):
        result = repeat.result if first.result == "passed" else first.result
        lines.append(f"| {first.name} | {first.seconds:.3f} | {repeat.seconds:.3f} | {result} |")
    notes = [f"- `{case.title}` {case.result}: {(case.message.splitlines() or [''])[0]}" for case in cases if case.result != "passed"]
    if notes:
        lines.extend(["", "### Details", "", *notes])
    summary = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(summary, encoding="utf-8")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(summary)


def without_marketplace_timestamps(data: bytes) -> bytes:
    """Drop the per-marketplace lastUpdated that the Claude CLI rewrites on every marketplace update; the rest is compared as normalized JSON (key order and spacing ignored)."""
    # Hand-built, reason: only this CLI-maintained field is volatile, so source, path and entry changes must still show.
    try:
        entries = json.loads(data)
    except ValueError:
        return data
    if not isinstance(entries, dict) or not all(isinstance(entry, dict) for entry in entries.values()):
        return data
    return json.dumps({name: {key: value for key, value in entry.items() if key != "lastUpdated"} for name, entry in entries.items()}, sort_keys=True).encode()


def snapshot(root: Path, excludes: list[str] | None = None, ignore_timestamps: list[str] | None = None) -> dict[str, str]:
    """Hash a tree so a repeat can name changed paths."""
    # Hand-built, reason: a path-by-path hash names unexpected repeat changes and excludes observed CLI runtime files.
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if any(re.fullmatch(pattern, relative) for pattern in excludes or []):
            continue
        if path.is_dir():
            result[relative] = "directory"
        else:
            data = path.read_bytes()
            result[relative] = hashlib.sha256(without_marketplace_timestamps(data) if relative in (ignore_timestamps or []) else data).hexdigest()
    return result


def unchanged(before: dict[str, str], after: dict[str, str]) -> None:
    changes = [f"{'added' if name not in before else 'removed' if name not in after else 'changed'}: {name}" for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
    if changes:
        omitted = f"\n{len(changes) - 20} more paths omitted" if len(changes) > 20 else ""
        raise AssertionError("the home changed on the repeat run:\n" + "\n".join(changes[:20]) + omitted)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def contains(output: str, expected: str) -> None:
    require(expected in output, f"expected {expected!r} in output:\n{output}")


# A detached shell deletes legion's folders once the legion command has exited.
def wait_gone(*paths: Path) -> None:
    deadline = time.monotonic() + 90
    while any(os.path.lexists(path) for path in paths) and time.monotonic() < deadline:
        time.sleep(0.5)
    for path in paths:
        require(not os.path.lexists(path), f"{path} remains after the uninstall handover")


@contextmanager
def restore_user_path():
    """Restore the Windows user PATH if an external CLI installer changes it."""
    # Hand-built, reason: winreg is the standard library API that preserves both the raw value and registry type.
    if sys.platform != "win32":
        yield
        return
    winreg = importlib.import_module("winreg")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        try:
            before = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            before = None
        try:
            yield
        finally:
            try:
                after = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                after = None
            if after != before:
                if before is None:
                    winreg.DeleteValue(key, "Path")
                else:
                    winreg.SetValueEx(key, "Path", 0, before[1], before[0])
                try:
                    restored = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    restored = None
                require(restored == before, "Windows user PATH was not restored")


def windows_saved_path_folders() -> list[Path]:
    """Find folders added by an installer after this process started."""
    # Hand-built, reason: winget saves PATH in the registry, while the CI process keeps its old copy.
    winreg = importlib.import_module("winreg")
    folders = []
    for root, key_name in ((winreg.HKEY_CURRENT_USER, "Environment"), (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key_name) as key:
                value = str(winreg.QueryValueEx(key, "Path")[0])
        except FileNotFoundError:
            continue
        folders.extend(Path(os.path.expandvars(part.strip().strip('"'))) for part in value.split(";") if part.strip())
    return folders


class Runner:
    def __init__(self, checkout: Path, home: Path, out: Path, fallback: bool, *, uv: str | None = None, client_path: str | None = None) -> None:
        self.checkout, self.home, self.out, self.fallback = checkout, home, out, fallback
        self.work = home.parent / "legion-work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.uv = self.home / ".local/bin" / ("uv.exe" if WINDOWS else "uv")
        self.uv.parent.mkdir(parents=True, exist_ok=True)
        if not self.uv.is_file():
            source = uv or shutil.which("uv")
            if source is None:
                raise AssertionError("uv is required on PATH")
            shutil.copy2(source, self.uv)
        self.tool_dir = self.home / "uv-tools"
        self.catalog = {entry["id"]: entry for entry in json.loads((checkout / "plugins/legion/tools.json").read_text(encoding="utf-8"))["tools"]}
        self.targets: dict[str, tuple[str, str]] = {}
        self.not_tested: dict[str, str] = {}
        self.versions: dict[str, tuple[str, str]] = {}
        self.state: dict[str, Any] = {}
        self.log: list[str] = []
        self.env = os.environ.copy()
        for name in ("LEGION_ROLE", "LEGION_TASK", "UV_INSTALL_DIR", "XDG_BIN_HOME"):
            self.env.pop(name, None)
        temp = self.work / "temp"
        temp.mkdir(exist_ok=True)
        self.env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "NO_COLOR": "1",
                "LEGION_TEST_USER_PATH_FILE": str(self.work / "user-path.txt"),
                "PSModuleAnalysisCachePath": str(self.work / "ps-module-cache"),
                "UV_TOOL_DIR": str(self.tool_dir),
                "UV_TOOL_BIN_DIR": str(self.uv.parent),
                "UV_CACHE_DIR": str(self.home / "uv-cache"),
                "UV_PYTHON_DOWNLOADS": "never",
                "TMP": str(temp),
                "TEMP": str(temp),
                "TMPDIR": str(temp),
            }
        )
        self.client_path = client_path if client_path is not None else self.env["PATH"]
        self.base_path = self.uv_path(self.client_path)
        self.env["PATH"] = self.base_path
        (self.work / "user-path.txt").touch(exist_ok=True)
        for tool in TOOLS:
            self.env[self.catalog[tool]["configEnv"]] = str(self.folder(tool))

    def folder(self, tool: str) -> Path:
        value = self.catalog[tool]["plugin"]["defaultConfigDir"]
        return self.home / value[2:] if value.startswith("~/") else Path(value)

    def uv_path(self, path: str) -> str:
        return os.pathsep.join((str(self.uv.parent), path))

    def nested(self, checkout: Path, home: Path) -> "Runner":
        # Native clients such as Claude can install beside the parent's uv.
        client_folders = [str(Path(command).parent) for _, command in self.versions.values()]
        client_path = os.pathsep.join((*dict.fromkeys(client_folders), self.client_path))
        runner = Runner(checkout, home, self.out, False, uv=str(self.uv), client_path=client_path)
        runner.log = self.log
        return runner

    def command(self, name: str) -> str:
        path = shutil.which(name, path=self.env["PATH"])
        if path:
            return path
        candidates = []
        if sys.platform == "win32":
            candidates += windows_saved_path_folders()
        for folder in candidates:
            if folder.is_dir():
                if str(folder) not in self.base_path.split(os.pathsep):
                    self.base_path += os.pathsep + str(folder)
                    self.client_path += os.pathsep + str(folder)
                if str(folder) not in self.env["PATH"].split(os.pathsep):
                    self.env["PATH"] += os.pathsep + str(folder)
        path = shutil.which(name, path=self.env["PATH"])
        if path is None:
            raise AssertionError(f"{name} was not found on PATH after install")
        return path

    def run(self, argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True, answers: str | None = None) -> str:
        stdin = subprocess.DEVNULL if answers is None else None
        result = subprocess.run(argv, cwd=cwd, env=env or self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=stdin, input=answers, check=False)  # noqa: S603
        output = result.stdout.rstrip()
        self.log.append(output)
        if check and result.returncode:
            print(f"{Path(argv[0]).name} exited {result.returncode}:", output, sep=chr(10), file=sys.stderr, flush=True)
            raise RuntimeError(f"{Path(argv[0]).name} exited {result.returncode}\n{output}")
        return output

    def legion(self, *args: str, owner: bool = False, answers: str | None = None) -> str:
        if owner:
            return self.run([str(self.legion_executable()), *args], answers=answers)
        env = self.env.copy()
        env["UV_PROJECT_ENVIRONMENT"] = str(self.work / "python-env")
        return self.run([str(self.uv), "run", "--project", str(self.checkout / "plugins/legion"), "--frozen", "--quiet", "legion", "--home", str(self.home), *args], env=env, answers=answers)

    def legion_executable(self) -> Path:
        folder = Path(self.run([str(self.uv), "tool", "dir", "--bin"]))
        require(folder.resolve() == self.uv.parent.resolve(), f"uv tool bin is outside the fixture: {folder}")
        return folder / ("legion.exe" if WINDOWS else "legion")

    def tool_version(self) -> str | None:
        output = self.run([str(self.uv), "tool", "list"])
        match = re.search(r"^legion v(\S+)", output, re.MULTILINE)
        return match[1] if match else None

    def check_tool(self, version: str) -> None:
        require(self.tool_version() == version, f"uv does not list legion {version}")
        require(self.legion("--version", owner=True) == f"legion {version}", f"the uv executable did not run legion {version}")

    def check_tool_removed(self) -> None:
        wait_gone(self.legion_executable(), self.tool_dir / "legion")
        require(self.tool_version() is None, "uv still lists legion after uninstall")
        self.run([str(self.uv), "--version"])

    # An account starts through the owner command; legion writes no per-account launcher.
    def account_run(self, account: str, *args: str) -> str:
        return self.legion("run", "--account", account, "--", *args, owner=True)

    def cli(self, tool: str, args: list[str], folder: Path | None = None) -> str:
        entry = self.catalog[tool]
        env = self.env.copy()
        env[entry["configEnv"]] = str(folder or self.folder(tool))
        env.update(entry["plugin"].get("listingEnv", {}))
        return self.run([self.command(entry["command"]), *args], env=env, cwd=self.work)

    def listing(self, tool: str, folder: Path, kind: str = "plugin", include_disabled: bool = False, *, wanted: str | None = None) -> bool:
        return listed(self.catalog[tool], self.listing_output(tool, folder, kind), include_disabled, kind, wanted=wanted)

    def listing_output(self, tool: str, folder: Path, kind: str = "plugin") -> str:
        args = self.catalog[tool]["plugin"]["marketplaceList" if kind == "marketplace" else "list"]
        return self.cli(tool, args, folder)

    def plugin_manifests(self, tool: str, folder: Path) -> list[Path]:
        entry = self.catalog[tool]
        root = entry["plugin"].get("installedRoot")
        files = [path for path in (folder / root).rglob("plugin.json") if path.parent.name not in (".claude-plugin", ".codex-plugin")] if root else []
        if tool == "copilot":
            try:
                settings = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
                marketplace = settings["extraKnownMarketplaces"]["ps-legion-marketplace"]
                if settings["enabledPlugins"].get("legion@ps-legion-marketplace") is not True:
                    return []
                if marketplace["source"]["source"] == "directory":
                    source = marketplace["source"]["path"]
                    manifest = json.loads((Path(source) / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
                    files = [Path(source) / plugin["source"] / "plugin.json" for plugin in manifest["plugins"] if plugin["name"] == "legion"]
            except (OSError, KeyError, ValueError):
                pass
        return files

    def version(self, tool: str, folder: Path) -> str | None:
        versions = []
        for file in self.plugin_manifests(tool, folder):
            try:
                data = json.loads(file.read_text(encoding="utf-8-sig"))
                if data.get("name") == "legion":
                    versions.append(data["version"])
            except (OSError, ValueError, KeyError):
                pass
        return max(versions, key=lambda value: tuple(int(part) for part in value.split(".")[:3]), default=None)

    def installed_source_matches(self, tool: str, folder: Path) -> bool:
        hook = "hooks/session_start.py"
        expected = (self.checkout / "plugins/legion" / hook).read_bytes()
        if tool == "copilot":
            try:
                settings = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
                source = settings["extraKnownMarketplaces"]["ps-legion-marketplace"]["source"]
                if self.targets[tool][1] == "main":
                    return source["source"] != "directory" and MARKETPLACE.lower() in json.dumps(source).lower()
                return source["source"] == "directory" and Path(source["path"]).resolve() == self.checkout.resolve()
            except (OSError, KeyError, ValueError):
                return False
        installed_root = self.catalog[tool]["plugin"].get("installedRoot")
        if not installed_root or not (folder / installed_root).is_dir():
            return False
        files = []
        for manifest in (folder / installed_root).rglob("plugin.json"):
            if manifest.parent.name in (".codex-plugin", ".claude-plugin"):
                continue
            try:
                if json.loads(manifest.read_text(encoding="utf-8-sig")).get("name") == "legion":
                    files.append(manifest.parent / hook)
            except (OSError, ValueError):
                continue
        return bool(files) and all(path.is_file() and path.read_bytes() == expected for path in files)

    def settings(self) -> dict[str, Any]:
        return json.loads((self.home / ".legion/configuration/personalisation.json").read_text(encoding="utf-8"))

    def active(self) -> list[str]:
        return [tool for tool in TOOLS if tool not in self.not_tested]

    def home_snapshot(self, update: bool = False) -> dict[str, str]:
        runtime = {"claude": [], "codex": ["tmp", r"tmp/arg0-(windows|linux|macos)(/codex-arg0[^/]+(/.*)?)?"], "copilot": ["logs", r"logs/process-[0-9]+-[0-9]+\.log"]}
        folders = [(tool, self.folder(tool)) for tool in TOOLS]
        if (self.home / ".legion/configuration/personalisation.json").exists():
            folders += [(agent["tool"], self.resolve_folder(agent.get("configDir", ""))) for agent in self.settings()["agents"]]
        # Hand-built, reason: CLI runtime caches in HOME are unrelated to Legion repeat behavior.
        result = {}
        legion = self.home / ".legion"
        nested = [re.escape(folder.relative_to(legion).as_posix()) + r"/.*" for _, folder in folders if folder.is_relative_to(legion) and folder != legion]
        for path in [legion, *(folder for _, folder in folders)]:
            if path.is_dir() and path.is_relative_to(self.home):
                prefix = path.relative_to(self.home).as_posix()
                tool = next((tool for tool, folder in folders if folder == path), None)
                if path == legion:
                    excludes = nested
                elif tool:
                    excludes = runtime[tool]
                else:
                    excludes = []
                ignore_timestamps = ["plugins/known_marketplaces.json"] if update and tool == "claude" else []
                for name, value in snapshot(path, excludes, ignore_timestamps).items():
                    result[prefix + "/" + name] = value
        for name in (".zshrc", ".bashrc", ".profile"):
            path = self.home / name
            if path.is_file():
                result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def resolve_folder(self, value: str) -> Path:
        return self.home / value[2:] if value.startswith("~/") else Path(value)

    def install_tool(self, tool: str) -> str:
        args = ("tool", "install", "--tool", tool, "--yes")
        try:
            return self.legion(*args)
        except RuntimeError as error:
            if not WINDOWS or not re.search(r"\bexited 2(?:\n|$)", str(error)):
                raise
            return self.legion(*args, "--method", "zip")

    def install_cli(self, tool: str, repeat: bool) -> str:
        output = self.install_tool(tool)
        if repeat:
            contains(output, "skip:")
        command = self.command(self.catalog[tool]["command"])
        version_output = self.run([command, "--version"])
        match = re.search(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version_output)
        if match is None:
            raise AssertionError(f"{tool} --version has no version token: {version_output}")
        value = (match.group(), command)
        if repeat:
            require(value == self.versions[tool], f"{tool} version or executable changed on repeat: {self.versions[tool]} -> {value}")
        else:
            self.versions[tool] = value
        return output + "\n" + version_output

    def bootstrap(self, repeat: bool) -> str:
        if sys.platform == "win32":
            output = self.run(["powershell.exe", "-NoProfile", "-File", str(self.checkout / "install.ps1"), "-DryRun", "-HomeRoot", str(self.home)])
        else:
            output = self.run(["sh", str(self.checkout / "install.sh"), "--dry-run"])
        contains(output, "skip: uv (already done)")
        require(self.tool_version() is None, "bootstrap preview populated the fresh uv tool fixture")
        return output + "\n" + self.run([str(self.uv), "--version"])

    def install_legion(self, source: str, ref: str = "release") -> str:
        return self.legion("install", "--source", source, "--ref", ref, "--yes")

    def local_source_refusals(self, output: str) -> set[str]:
        # Install continues past failed targets. Only an explicit local-source refusal can be skipped.
        failed = [line.split() for line in output.splitlines() if "  failed: " in line]
        refused = set()
        for row in failed:
            tool = row[0]
            command = self.catalog.get(tool, {}).get("command")
            reason = " ".join(row).split(" failed: ", 1)[-1]
            refusal = re.search(
                r"(local (path|folder|directory|source|marketplace)|filesystem|directory source).{0,80}(not supported|unsupported|not allowed|not permitted|cannot|refus|rejected)",
                reason,
                re.IGNORECASE,
            )
            require(bool(command and re.match(rf"{re.escape(command)} exited \d+", reason) and refusal), output)
            refused.add(tool)
        return refused

    def install_targets(self, *, repeat: bool) -> str:
        source = str(self.checkout)
        try:
            output = self.install_legion(source)
        except RuntimeError as error:
            output = str(error)
            refused = self.local_source_refusals(output)
            if not refused or any(tool not in self.not_tested and self.targets[tool][1] != "main" for tool in refused):
                raise
            if repeat:
                contains(output, f"skip: legion command {self.tool_version()} (already done)")
            return output
        if repeat:
            require(output == INSTALLATION_NO_CHANGES, output)
        return output

    def install_first(self) -> str:
        source = str(self.checkout)
        self.targets.update(dict.fromkeys(TOOLS, (source, "release")))
        try:
            output = self.install_legion(source)
        except RuntimeError as error:
            output = str(error)
            refused = self.local_source_refusals(output)
            if not refused:
                raise
            if self.fallback:
                revision = self.run(["git", "-C", source, "rev-parse", "HEAD"])
                remote = self.run(["git", "-C", source, "ls-remote", "origin", "refs/heads/main"]).split()[0]
                require(remote == revision, f"Published source needs main to match the checkout: {revision}, main {remote}")
                output += "\n" + self.install_legion(MARKETPLACE, "main")
                later = self.run(["git", "-C", source, "ls-remote", "origin", "refs/heads/main"]).split()[0]
                require(later == revision, f"main moved during install: {revision} -> {later}")
                self.targets.update(dict.fromkeys(refused, (MARKETPLACE, "main")))
            else:
                self.not_tested.update({tool: f"{tool} refuses a local marketplace folder" for tool in refused})
                output += "".join(f"\nskip: {self.not_tested[tool]}" for tool in sorted(refused))
        require(bool(self.active()), "; ".join(self.not_tested.values()))
        expected = json.loads((self.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
        self.check_tool(expected)
        for tool in self.active():
            if not self.listing(tool, self.folder(tool)):
                shown = "\n".join(self.listing_output(tool, self.folder(tool)).splitlines()[:20])
                raise AssertionError(f"{tool} does not list enabled legion; the listing was:\n{shown}")
            control = self.work / f"control-{tool}"
            control.mkdir(exist_ok=True)
            require(not self.listing(tool, control, include_disabled=True), f"{tool} lists legion in empty control folder")
            require(self.version(tool, self.folder(tool)) == expected, f"{tool} plugin version is wrong")
        if "codex" in self.active():
            self.check_codex_mcp()
        self.legion("status")
        self.state["snapshot"] = self.home_snapshot()
        return output

    def check_codex_mcp(self) -> None:
        """The installed plugin must bring a connected legion MCP server that Codex lists, asked without login or model turn."""
        require(self.legion_executable().is_file(), "the uv executable is missing")
        # The app-server starts a background clone of Codex's curated marketplace, and Codex has no setting to stop it,
        # so the probe works on a throwaway copy and nothing it writes reaches the snapshotted home.
        probe_home = Path(tempfile.mkdtemp(prefix="codex-probe-home-", dir=self.work))
        shutil.copytree(self.folder("codex"), probe_home, dirs_exist_ok=True)
        env = {**self.env, self.catalog["codex"]["configEnv"]: str(probe_home)}
        try:
            servers = probe_codex_mcp([self.command("codex"), "app-server"], env, self.work, CODEX_PROBE_SECONDS)
        finally:
            shutil.rmtree(probe_home, ignore_errors=True)
        self.log.append(json.dumps(servers))
        require(codex_legion_server(servers) is not None, "Codex does not list a connected legion MCP server with the status tool")

    def install_repeat(self) -> str:
        output = self.install_targets(repeat=True)
        unchanged(self.state["snapshot"], self.home_snapshot())
        return output

    def update(self, repeat: bool) -> str:
        expected = json.loads((self.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
        active = self.active()
        if not repeat:
            # Use the public tool commands to leave one target absent. The owner update must restore it.
            target = next((tool for tool in active if self.targets[tool][1] != "main"), active[0])
            entry = self.catalog[target]["plugin"]
            self.cli(target, entry["uninstall"])
            self.cli(target, entry["marketplaceRemove"])
            require(not self.listing(target, self.folder(target)), f"{target} remains installed before update")
            self.state["update_target"] = target
        target = self.state["update_target"]
        if repeat:
            output = self.legion("update", "--yes", owner=True)
        else:
            source, ref = self.targets[target]
            arguments = ["update", "--source", source]
            if ref == "main":
                arguments += ["--ref", "main"]
            try:
                output = self.legion(*arguments, "--yes", owner=True)
            except RuntimeError as error:
                output = str(error)
                refused = self.local_source_refusals(output)
                if not refused or refused - self.not_tested.keys():
                    raise
        if not repeat:
            restored = rf"^{re.escape(target)}\s+{re.escape(str(self.folder(target)))}\s+installed legion {re.escape(expected)}$"
            require(re.search(restored, output, re.MULTILINE) is not None, output)
        elif all(self.targets[tool][1] != "main" for tool in active):
            require(output == UPDATE_NO_CHANGES, output)
        else:
            contains(output, "already done")
        for tool in active:
            if not repeat:
                contains(output, f"Update legion for {tool}")
            require(self.version(tool, self.folder(tool)) == expected, f"{tool} version changed away from the checkout")
            require(self.installed_source_matches(tool, self.folder(tool)), f"{tool} update did not keep the PR checkout source")
        if repeat:
            unchanged(self.state["update_snapshot"], self.home_snapshot(update=True))
        else:
            self.state["update_snapshot"] = self.home_snapshot(update=True)
        self.check_tool(expected)
        return output

    def stage_candidate(self, release: Path, current: str, pin: str) -> str:
        plugin = release / "plugins/legion"
        major, minor, patch = map(int, re.split(r"[-+]", current, maxsplit=1)[0].split("."))
        version = f"{major}.{minor}.{patch + 1}"
        for name in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            path = plugin / name
            document = json.loads(path.read_text(encoding="utf-8"))
            document["version"] = version
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.run([str(self.uv), "lock", "--project", str(plugin), "--upgrade-package", f"click<{pin}"])
        git = ["git", "-C", str(release)]
        self.run([*git, "add", "--", str(plugin)])
        identity = ["-c", "user.name=Integration fixture", "-c", "user.email=integration@example.invalid"]
        self.run([*git, *identity, "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Stage candidate"])
        return version

    def click_pin(self) -> str:
        lock = tomllib.loads((self.checkout / "plugins/legion/uv.lock").read_text(encoding="utf-8"))
        return next(package["version"] for package in lock["package"] if package["name"] == "click")

    def installed_click(self) -> str:
        python = self.tool_dir / ("legion/Scripts/python.exe" if WINDOWS else "legion/bin/python")
        return self.run([str(python), "-I", "-c", "from importlib.metadata import version; print(version('click'))"])

    def wait_tool_version(self, version: str) -> None:
        deadline = time.monotonic() + 90
        stop = self.home / ".legion/run/stop"
        receipt = self.tool_dir / "legion/uv-receipt.toml"
        while stop.exists() or self.tool_version() != version or not self.legion_executable().is_file() or not receipt.is_file():
            require(time.monotonic() < deadline, f"the detached uv install did not complete for legion {version}")
            time.sleep(0.5)

    def update_uv_tool(self, repeat: bool) -> None:
        if repeat:
            upgrade, version = self.state["uv_upgrade"]
            upgrade.log = self.log
            upgrade.repeat_uv_update_and_purge(version)
            return
        root = self.work / "p3"
        release = root / "release"
        remote = f"https://github.com/{MARKETPLACE}"
        root.mkdir()
        self.run(["git", "clone", "--quiet", "--no-local", "--depth", "1", "--config", "core.longpaths=true", str(self.checkout), str(release)])
        git = ["git", "-C", str(release)]
        self.run([*git, "switch", "--quiet", "-C", "stable"])
        upgrade = self.nested(release, root / "home")
        current = json.loads((release / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
        pin = upgrade.click_pin()
        # Host marketplace commands use both Git URL spellings.
        rewrite = f"url.{release.resolve().as_uri()}.insteadOf"
        upgrade.env.update(
            {
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": rewrite,
                "GIT_CONFIG_VALUE_0": remote + ".git",
                "GIT_CONFIG_KEY_1": rewrite,
                "GIT_CONFIG_VALUE_1": remote,
                "GIT_CONFIG_KEY_2": "core.longpaths",
                "GIT_CONFIG_VALUE_2": "true",
            }
        )
        # Copilot fetches GitHub marketplaces itself. Register its live directory marketplace first.
        copilot = upgrade.catalog["copilot"]["plugin"]
        upgrade.cli("copilot", [word.replace("{source}", release.as_posix()) for word in copilot["marketplaceAdd"]])
        upgrade.install_legion(MARKETPLACE)
        for tool in TOOLS:
            require(upgrade.version(tool, upgrade.folder(tool)) == current, f"{tool} did not install {current}")
        upgrade.check_tool(current)
        require(upgrade.installed_click() == pin, f"legion {current} did not install Click {pin}")

        version = upgrade.stage_candidate(release, current, pin)
        updated_pin = upgrade.click_pin()
        require(updated_pin != pin, f"candidate {version} still locks Click {pin}")
        output = upgrade.legion("update", "--yes", owner=True)
        contains(output, f"legion {current} -> {version}")
        for tool in TOOLS:
            require(upgrade.version(tool, upgrade.folder(tool)) == version, f"{tool} did not update to {version}")
        upgrade.wait_tool_version(version)
        upgrade.check_tool(version)
        require(upgrade.installed_click() == updated_pin, f"legion {version} did not install Click {updated_pin}")
        status = json.loads(upgrade.legion("status", "--json", owner=True))
        require(Path(status["settings"]["path"]).resolve() == (upgrade.home / ".legion/configuration/personalisation.json").resolve(), "status used the wrong home")

        # Run the MCP client in the original checkout's project environment.
        client = """
import asyncio
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    server = StdioServerParameters(command=sys.argv[1], args=["mcp"], env=env)
    async with asyncio.timeout(60), stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("status")
        if result.isError:
            raise RuntimeError(str(result))
        print(json.dumps(result.structuredContent))

asyncio.run(main())
"""
        mcp = upgrade.run(
            [str(upgrade.uv), "run", "--project", str(self.checkout / "plugins/legion"), "--frozen", "--extra", "mcp", "--quiet", "python", "-c", client, str(upgrade.legion_executable())],
            env={**upgrade.env, "UV_PROJECT_ENVIRONMENT": str(upgrade.work / "mcp-client-env")},
        )
        require(json.loads(mcp) == status, "MCP status differs from CLI status")
        upgrade.check_codex_mcp()

        hook_env = {**upgrade.env, "LEGION_ROLE": "member", "LEGION_TASK": "upgrade-fixture"}
        for tool in TOOLS:
            manifests = upgrade.plugin_manifests(tool, upgrade.folder(tool))
            candidates = [path for path in manifests if json.loads(path.read_text(encoding="utf-8-sig")).get("version") == version]
            require(bool(candidates), f"no {tool} candidate plugin manifest for the hook check")
            hook = candidates[0].parent / "hooks/session_start.py"
            reply = upgrade.run([sys.executable, "-I", "-S", "-B", str(hook), "--format", "context-json"], env=hook_env)
            contains(json.loads(reply)["additionalContext"], "you are a legion member")

        self.state["uv_upgrade"] = (upgrade, version)

    def uv_tool_snapshot(self) -> dict[str, str]:
        # Bytecode caches may be populated by Python on the repeat. Receipt and launcher timestamps detect a reinstall.
        state = snapshot(self.tool_dir, [r"(.*/)?__pycache__(/.*)?"])
        state["receipt timestamp"] = str((self.tool_dir / "legion/uv-receipt.toml").stat().st_mtime_ns)
        state["launcher timestamp"] = str(self.legion_executable().stat().st_mtime_ns)
        return state

    def repeat_uv_update_and_purge(self, version: str) -> None:
        before = self.uv_tool_snapshot()
        output = self.legion("update", "--yes", owner=True)
        contains(output, f"skip: legion command {version} (already done)")
        unchanged(before, self.uv_tool_snapshot())
        require(not (self.home / ".legion/run/stop").exists(), "the repeat started a detached replacement")

        self.legion("uninstall", "--purge", "--yes", owner=True)
        wait_gone(self.home / ".legion")
        self.check_tool_removed()
        for tool in TOOLS:
            folder = self.folder(tool)
            self.check_uninstalled(tool, folder)
            require(not self.listing(tool, folder, "marketplace"), f"{tool} marketplace remains")

    def setup_accounts(self) -> str:
        return "\n".join(self.legion("add", tool, "--id", SETUP_ACCOUNTS[tool], "--source", self.targets[tool][0], "--yes") for tool in self.active())

    def check_accounts(self, agents: list[dict[str, Any]]) -> None:
        expected = json.loads((self.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
        for agent in agents:
            require(self.version(agent["tool"], self.resolve_folder(agent["configDir"])) == expected, f"{agent['id']} has wrong legion version")

    def setup_first(self) -> str:
        output = self.setup_accounts()
        settings_path = self.home / ".legion/configuration/personalisation.json"
        self.legion("validate", "--kind", "personalisation", "--path", str(settings_path), owner=True)
        require((settings_path.parent / "notes.md").is_file(), "notes.md missing")
        agents = self.settings()["agents"]
        for tool in self.active():
            account = SETUP_ACCOUNTS[tool]
            require(account in [agent["id"] for agent in agents], f"{account} missing")
            folder = self.resolve_folder(next(agent["configDir"] for agent in agents if agent["id"] == account))
            variable = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "copilot": "COPILOT_HOME"}[tool]
            start = f"$env:{variable}='{folder}'; {tool}" if sys.platform == "win32" else f"{variable}='{folder}' {tool}"
            contains(output, f"log in: {start}")
        self.check_accounts(agents)
        help_text = self.legion("--help", owner=True)
        for verb in ("inbox", "telegram", "validate", "setup", "status"):
            contains(help_text, f"  {verb} ")
        self.state["snapshot"] = self.home_snapshot()
        return output + "\n" + help_text

    def setup_repeat(self) -> str:
        output = self.setup_accounts()
        contains(output, "skip:")
        unchanged(self.state["snapshot"], self.home_snapshot())
        return output

    def add(self, repeat: bool) -> str:
        tool = self.active()[0]
        output = self.legion("add", tool, "--id", "ci-extra", "--source", self.targets[tool][0], "--yes")
        agents = self.settings()["agents"]
        if repeat:
            contains(output, "skip: account ci-extra (already done)")
            require(len(agents) == self.state["agent_count"], "repeat added an account")
        else:
            matched = [agent for agent in agents if agent["id"] == "ci-extra"]
            require(len(matched) == 1, "ci-extra not recorded")
            require(self.resolve_folder(matched[0]["configDir"]).is_dir(), "ci-extra folder missing")
            self.check_accounts(matched)
            self.state["agent_count"] = len(agents)
        return output

    def remove(self, repeat: bool) -> str:
        folder = self.resolve_folder(next(agent["configDir"] for agent in self.settings()["agents"] if agent["id"] == "ci-extra")) if not repeat else None
        output = self.legion("remove", "--id", "ci-extra", owner=True)
        if repeat:
            contains(output, "skip: account remove ci-extra (already done)")
        else:
            require(not any(agent["id"] == "ci-extra" for agent in self.settings()["agents"]), "ci-extra still in settings")
            require(folder is not None and folder.is_dir(), "ci-extra folder removed")
            contains(output, "Config folder kept at")
        return output

    def new_repo(self, name: str, rules: bool = False) -> Path:
        repo = self.work / name
        repo.mkdir(exist_ok=True)
        self.run(["git", "-C", str(repo), "init", "--quiet"])
        self.run(["git", "-C", str(repo), "config", "user.name", "integration"])
        self.run(["git", "-C", str(repo), "config", "user.email", "integration@example.com"])
        self.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"])
        (repo / "README.md").write_text(name + "\n", encoding="utf-8")
        self.run(["git", "-C", str(repo), "add", "README.md"])
        if rules:
            (repo / "AGENTS.md").write_text(f"# {name} rules\n", encoding="utf-8")
            self.run(["git", "-C", str(repo), "add", "AGENTS.md"])
        self.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "initial"])
        return repo

    def commit_count(self, repo: Path) -> int:
        return int(self.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"]))

    def repository(self, mode: str, repeat: bool) -> str:
        name = "repo-a" if mode == "private" else "repo-b"
        repo = self.work / name if repeat else self.new_repo(name, rules=mode == "shared")
        args = ["repository", "setup", "--repo", str(repo), "--mode", mode, "--gate", "git status --porcelain"]
        if mode == "shared":
            args += ["--rules-file", "AGENTS.md"]
        output = self.legion(*args, owner=True)
        for item in ("configuration/project.json", "configuration/workflow.md", "scripts/index.md", "scripts/check-task.ps1", "state/handoff.md"):
            require((repo / ".legion" / item).exists(), f".legion/{item} missing")
        self.legion("validate", "--kind", "project", "--path", str(repo / ".legion/configuration/project.json"), owner=True)
        if mode == "private":
            require(self.commit_count(repo / ".legion") >= 1, "inner repository has no commit")
            require(self.commit_count(repo) == 1 and not self.run(["git", "-C", str(repo), "status", "--porcelain"]), "outer repository changed")
            if repeat:
                contains(output, "skip: Private orchestration (already done)")
                require(self.commit_count(repo / ".legion") == self.state["inner_commits"], "repeat made an inner commit")
            else:
                self.state["inner_commits"] = self.commit_count(repo / ".legion")
        else:
            heading = (self.checkout / "plugins/legion/skills/legion-repository/references/rules-file-lines.md").read_text(encoding="utf-8").splitlines()[0]
            require((repo / "AGENTS.md").read_text(encoding="utf-8").splitlines().count(heading) == 1, "rules block count wrong")
            require(self.commit_count(repo) == 2, "shared repository commit count wrong")
            require(self.run(["git", "-C", str(repo), "log", "-1", "--format=%s"]) == "Set up legion orchestration", "unexpected commit subject")
            require(not self.run(["git", "-C", str(repo), "status", "--porcelain"]), "shared repository dirty")
            if repeat:
                contains(output, "skip:")
        return output

    def channel_account(self) -> str | None:
        for agent in self.settings()["agents"]:
            if agent["id"] in (SETUP_ACCOUNTS["claude"], SETUP_ACCOUNTS["codex"]):
                return agent["id"]
        require("claude" in self.not_tested and "codex" in self.not_tested, "no Claude or Codex setup account")
        return None

    def channel(self, repeat: bool) -> str:
        account = self.channel_account()
        if account is None:
            return "not tested: no Claude or Codex setup account"
        add = ("channel", "add", "--id", "ci-bot", "--kind", "telegram", "--target", "1", "--account", account)
        lines = [self.legion(*add, owner=True), self.legion("channel", "check", "--kind", "telegram", "--account", account, owner=True)]
        if repeat:
            again = self.legion(*add, owner=True)
            contains(again, "skip:")
            lines.append(again)
        require("ci-bot" in [channel["id"] for channel in self.settings().get("channels", [])], "ci-bot binding missing")
        lines.append(self.legion("channel", "remove", "--id", "ci-bot", owner=True))
        if repeat:
            removed = self.legion("channel", "remove", "--id", "ci-bot", owner=True)
            contains(removed, "skip:")
            lines.append(removed)
        require("ci-bot" not in [channel["id"] for channel in self.settings().get("channels", [])], "ci-bot binding remains")
        return "\n".join(lines)

    @contextmanager
    def fake_az(self):
        old = self.env["PATH"]
        self.env["PATH"] = self.uv_path(os.pathsep.join((str(self.checkout / "tests/fixtures/integration-az"), old.partition(os.pathsep)[2])))
        try:
            yield
        finally:
            self.env["PATH"] = old

    def foundry_launch(self) -> str:
        output = self.account_run("ci-foundry", "--version")
        require(not list((self.home / ".legion/temp").glob("copilot-providers-*.json")), "Copilot provider file remains")
        return output

    def foundry(self, repeat: bool) -> str:
        if "copilot" in self.not_tested:
            return "not tested: Copilot was not tested from checkout"
        args = ("account", "add", "--id", "ci-foundry", "--tool", "copilot", "--provider", "foundry", "--endpoint", ENDPOINT, "--deployment", "model-a", "--default", "model-a")
        with self.fake_az():
            output = self.legion(*args, owner=True)
            if repeat:
                contains(output, "skip: account ci-foundry (already done)")
            output += "\n" + self.foundry_launch()
            if repeat:
                output += "\n" + self.legion("remove", "--id", "ci-foundry", owner=True)
        return output

    @contextmanager
    def without_pwsh(self):
        # Hand-built, reason: an executable trap proves a child did not invoke pwsh.
        trap = self.work / "pwsh-trap"
        trap.mkdir(exist_ok=True)
        marker = trap / "called"
        marker.unlink(missing_ok=True)
        if sys.platform == "win32":
            (trap / "pwsh.cmd").write_text(f'@echo off\necho called>"{marker}"\nexit /b 97\n', encoding="utf-8")
        else:
            executable = trap / "pwsh"
            executable.write_text(f"#!/bin/sh\necho called > {shlex.quote(str(marker))}\nexit 97\n", encoding="utf-8")
            executable.chmod(0o755)
        kept = []
        for folder in self.client_path.split(os.pathsep):
            if not folder:
                continue
            path = Path(folder)
            if (
                sys.platform != "win32"
                or not (path / "pwsh.exe").exists()
                or any(
                    (path / (name + suffix)).exists() for name in ("uv", "git", "sh", "claude", "codex", "copilot", "legion", "node", "npm", "npx", "winget", "az") for suffix in ("", ".exe", ".cmd")
                )
            ):
                kept.append(folder)
        old = self.env["PATH"]
        restore_base = old == self.base_path
        self.env["PATH"] = self.uv_path(os.pathsep.join([str(trap), str(self.checkout / "tests/fixtures/integration-az"), *kept]))
        try:
            yield
        finally:
            self.env["PATH"] = self.base_path if restore_base else old
            require(not marker.exists(), "a command called pwsh")

    def no_pwsh(self, repeat: bool) -> str:
        with self.without_pwsh():
            repo = self.work / "no-pwsh-repo" if repeat else self.new_repo("no-pwsh-repo")
            lines = [self.legion("repository", "setup", "--repo", str(repo), "--gate", "git status --porcelain", owner=True)]
            if repeat:
                contains(lines[0], "skip:")
            account = self.channel_account()
            if account:
                add = ("channel", "add", "--id", "ci-no-pwsh", "--kind", "telegram", "--target", "1", "--account", account)
                lines.append(self.legion(*add, owner=True))
                if repeat:
                    again = self.legion(*add, owner=True)
                    contains(again, "skip:")
                    lines.append(again)
                lines += [self.legion("channel", "check", "--kind", "telegram", "--account", account, owner=True), self.legion("channel", "remove", "--id", "ci-no-pwsh", owner=True)]
            tool = self.active()[0]
            add_account = ("account", "add", "--id", "ci-no-pwsh", "--tool", tool)
            lines.append(self.legion(*add_account, owner=True))
            if repeat:
                again = self.legion(*add_account, owner=True)
                contains(again, "skip:")
                lines.append(again)
            lines.append(self.account_run("ci-no-pwsh", "--version"))
            lines.append(self.legion("remove", "--id", "ci-no-pwsh", owner=True))
            if "copilot" not in self.not_tested:
                with self.fake_az():
                    lines.append(
                        self.legion(
                            "account", "add", "--id", "ci-foundry", "--tool", "copilot", "--provider", "foundry", "--endpoint", ENDPOINT, "--deployment", "model-a", "--default", "model-a", owner=True
                        )
                    )
                    lines.append(self.foundry_launch())
                    lines.append(self.legion("remove", "--id", "ci-foundry", owner=True))
            return "\n".join(lines)

    def targets_for_uninstall(self, all_tools: bool = False) -> list[tuple[str, Path]]:
        active = list(TOOLS) if all_tools else self.active()
        targets = [(tool, self.folder(tool)) for tool in active]
        for agent in self.settings()["agents"]:
            if agent["tool"] in active and agent.get("configDir"):
                item = (agent["tool"], self.resolve_folder(agent["configDir"]))
                if item not in targets:
                    targets.append(item)
        return targets

    def check_uninstalled(self, tool: str, folder: Path) -> None:
        require(not self.listing(tool, folder, include_disabled=True), f"{tool} still lists legion in {folder}")
        require(not self.listing(tool, folder, "marketplace"), f"{tool} marketplace remains in {folder}")

    def uninstall(self, repeat: bool) -> str:
        targets = self.targets_for_uninstall() if not repeat else []
        output = self.legion("uninstall", "--yes", owner=not repeat)
        self.check_tool_removed()
        if repeat:
            contains(output, "skip: uninstall (already done)")
            return output
        for tool, folder in targets:
            self.check_uninstalled(tool, folder)
        for tool in self.active():
            self.run([self.command(self.catalog[tool]["command"]), "--version"])
        for name in ("configuration", "agents"):
            require((self.home / ".legion" / name).is_dir(), f"{name} removed")
        return output

    def reinstall(self, repeat: bool) -> str:
        output = self.install_targets(repeat=repeat)
        if any(ref == "main" for _, ref in self.targets.values()) and not repeat:
            output += "\n" + self.install_legion(MARKETPLACE, "main")
        expected = json.loads((self.checkout / "plugins/legion/plugin.json").read_text(encoding="utf-8"))["version"]
        self.check_tool(expected)
        if repeat:
            unchanged(self.state["snapshot"], self.home_snapshot())
        else:
            for tool, folder in self.targets_for_uninstall():
                require(self.version(tool, folder) == expected, f"{tool} version wrong in {folder}")
            for agent in self.settings()["agents"]:
                if agent["tool"] in self.active():
                    self.account_run(agent["id"], "--version")
            self.state["snapshot"] = self.home_snapshot()
        return output

    def purge(self, repeat: bool) -> str:
        if not repeat:
            self.state["purge_targets"] = self.targets_for_uninstall(all_tools=True)
            output = self.legion("uninstall", "--purge", "--yes", owner=True)
        else:
            output = self.legion("uninstall", "--purge", "--yes")
        if repeat:
            contains(output, "skip: purge (already done)")
        wait_gone(self.home / ".legion")
        self.check_tool_removed()
        for tool, folder in self.state["purge_targets"]:
            if folder.is_dir():
                self.check_uninstalled(tool, folder)
                require(not self.listing(tool, folder, "marketplace"), f"{tool} marketplace remains")
        for tool in TOOLS:
            self.cli(tool, ["--version"])
        if repeat:
            unchanged(self.state["purge_snapshot"], self.home_snapshot())
        else:
            self.state["purge_snapshot"] = self.home_snapshot()
        return output

    def knowledge_runner(self) -> "Runner":
        remote = f"https://github.com/{WIKI_SOURCE}"
        source = self.work / WIKI_MARKETPLACE
        git = ["git", "-C", str(source)]
        self.run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", WIKI_TAG, "--config", "core.longpaths=true", remote, str(source)])
        require(self.run([*git, "rev-parse", "HEAD"]) == WIKI_COMMIT, f"{WIKI_SOURCE} {WIKI_TAG} moved")
        self.run([*git, "tag", "--force", WIKI_TAG, WIKI_COMMIT])
        self.run([*git, "switch", "--quiet", "-c", "fixture-default"])

        wiki = self.nested(self.checkout, self.work / "knowledge" / "home")
        for tool in TOOLS:
            output = wiki.legion("add", tool, "--id", f"wiki-{tool}", "--source", str(self.checkout), "--yes")
            require("uv is missing" not in output, f"uv is missing while adding wiki-{tool} in the knowledge home")
        require({agent["tool"] for agent in wiki.settings()["agents"]} == set(TOOLS), "knowledge must test every CLI")
        rewrite = f"url.{source.resolve().as_uri()}.insteadOf"
        wiki.env.update(
            {
                "GIT_ALLOW_PROTOCOL": "file",
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": rewrite,
                "GIT_CONFIG_VALUE_0": remote + ".git",
                "GIT_CONFIG_KEY_1": rewrite,
                "GIT_CONFIG_VALUE_1": remote,
                "GIT_CONFIG_KEY_2": "core.longpaths",
                "GIT_CONFIG_VALUE_2": "true",
            }
        )
        return wiki

    def wiki_accounts(self) -> list[tuple[str, Path]]:
        return [(agent["tool"], self.resolve_folder(agent["configDir"])) for agent in self.settings()["agents"]]

    def wiki_ref(self, tool: str, folder: Path) -> object:
        output = json.loads(self.listing_output(tool, folder, "marketplace"))
        rows = output["marketplaces"] if tool == "codex" else output
        row = next((row for row in rows if row["name"] == WIKI_MARKETPLACE), None)
        if row is None:
            raise AssertionError(f"{tool} did not register {WIKI_MARKETPLACE}")
        require(WIKI_SOURCE in json.dumps(row), f"{tool} registered the wrong wiki source")
        if tool == "codex":
            return tomllib.loads((folder / "config.toml").read_text(encoding="utf-8-sig"))["marketplaces"][WIKI_MARKETPLACE].get("ref")
        if tool == "copilot":
            settings = json.loads((folder / "settings.json").read_text(encoding="utf-8-sig"))
            return settings["extraKnownMarketplaces"][WIKI_MARKETPLACE]["source"].get("ref")
        return row.get("ref")

    def check_wiki(self, ref: str | None) -> None:
        for tool, folder in self.wiki_accounts():
            require(self.listing(tool, folder, wanted=WIKI_PLUGIN), f"{tool} did not enable {WIKI_PLUGIN}")
            actual = self.wiki_ref(tool, folder)
            require(actual == ref, f"{tool} wiki ref is {actual!r}, expected {ref!r}")
            if tool == "copilot":
                require(any((folder / "installed-plugins/llm-wiki/wiki/skills").glob("*/SKILL.md")), "Copilot did not install the wiki skill")

    def manual_wiki(self) -> None:
        names = {"legion@ps-legion-marketplace": WIKI_PLUGIN, "{source}": WIKI_SOURCE}
        for tool, folder in self.wiki_accounts():
            recipe = self.catalog[tool]["plugin"]
            for step in ("marketplaceAdd", "install"):
                arguments: list[str] = recipe[step]
                self.cli(tool, [names.get(word, word) for word in arguments], folder)
        self.check_wiki(None)

    def knowledge_setup(self, repeat: bool) -> str:
        if not repeat:
            require("knowledgeEnabled" not in self.settings(), "knowledge choice must be unset before manual installation")
            self.manual_wiki()
        hub = self.home / ".legion/knowledge"
        pointer = self.home / ".config/llm-wiki/config.json"
        before = self.home_snapshot() if repeat else {}
        pointer_before = pointer.read_bytes() if repeat else b""
        output = self.legion("setup", "--knowledge", "--yes")
        if repeat:
            contains(output, "skip: knowledge hub (already done)")
            contains(output, "skip: llm-wiki hub pointer (already done)")
            skips = [line for line in output.splitlines() if line.startswith("skip: ") and WIKI_PLUGIN in line]
            require(len(skips) == len(self.wiki_accounts()), "expected a wiki install skip for every account")
            unchanged(before, self.home_snapshot())
            require(pointer.read_bytes() == pointer_before, "wiki pointer changed on repeat")
        self.check_wiki(WIKI_TAG)
        require(json.loads(pointer.read_text(encoding="utf-8"))["hub_path"] == "~/.legion/knowledge", "wrong wiki hub pointer")
        require((hub / "topics").is_dir() and all((hub / name).is_file() for name in ("wikis.json", "_index.md", "log.md")), "wiki hub files missing")
        status = json.loads(self.legion("status", "--json"))["knowledge"]
        require(status == {"state": "on", "hub": "ready", "hub_repository": True, "missing_accounts": []}, f"unexpected wiki status: {status}")
        return output

    def check_wiki_removed(self) -> None:
        for tool, folder in self.wiki_accounts():
            require(not self.listing(tool, folder, include_disabled=True, wanted=WIKI_PLUGIN), f"{tool} still lists the wiki plugin")
            require(not self.listing(tool, folder, "marketplace", wanted=WIKI_MARKETPLACE), f"{tool} still lists the wiki marketplace")

    def restore_wiki_accounts(self) -> None:
        # Restore the checkout copies before setup so it can apply the knowledge choice offline.
        self.install_legion(str(self.checkout))
        for tool, folder in self.wiki_accounts():
            require(self.listing(tool, folder), f"{tool} did not reinstall Legion in {folder}")

    def knowledge_off(self, repeat: bool) -> str:
        if not repeat:
            self.restore_wiki_accounts()
            self.legion("setup", "--knowledge", "--yes")
            self.check_wiki(WIKI_TAG)
        hub = self.home / ".legion/knowledge"
        pointer = self.home / ".config/llm-wiki/config.json"
        before = self.home_snapshot() if repeat else snapshot(hub)
        pointer_before = pointer.read_bytes()
        output = self.legion("setup", "--no-knowledge", "--yes")
        if repeat:
            contains(output, "skip: knowledge declined (already done)")
            unchanged(before, self.home_snapshot())
        else:
            unchanged(before, snapshot(hub))
        require(hub.is_dir(), "disabling knowledge removed the wiki hub")
        require(pointer.read_bytes() == pointer_before, "disabling knowledge changed the wiki pointer")
        self.check_wiki_removed()
        status = json.loads(self.legion("status", "--json"))["knowledge"]
        require(status["state"] == "declined", f"wiki choice was not declined: {status}")
        return output

    def knowledge_uninstall(self, repeat: bool) -> str:
        hub = self.home / ".legion/knowledge"
        before = snapshot(hub)
        output = self.legion("uninstall", "--yes", owner=not repeat)
        if repeat:
            contains(output, "skip: uninstall (already done)")
        self.check_wiki_removed()
        self.check_tool_removed()
        require(hub.is_dir(), "uninstall removed the wiki hub")
        unchanged(before, snapshot(hub))
        return output

    def knowledge_declined(self, repeat: bool) -> str:
        if not repeat:
            self.restore_wiki_accounts()
            self.legion("setup", "--no-knowledge", "--yes")
            self.manual_wiki()
        status = json.loads(self.legion("status", "--json"))["knowledge"]
        require(status["state"] == "declined", f"wiki choice was not declined: {status}")
        output = self.legion("uninstall", "--yes")
        for tool, folder in self.wiki_accounts():
            self.check_uninstalled(tool, folder)
        self.check_tool_removed()
        self.check_wiki(None)
        return output

    def knowledge_purge(self, repeat: bool) -> str:
        hub = self.home / ".legion/knowledge"
        before = snapshot(hub)
        output = self.legion("uninstall", "--purge", answers="y\nn\n")
        contains(output, "Apply? [y/N]")
        contains(output, "Also delete the knowledge hub in ~/.legion/knowledge? [y/N]")
        wait_gone(self.home / ".legion/configuration")
        self.check_tool_removed()
        require(hub.is_dir(), "purge removed the wiki hub after a decline")
        unchanged(before, snapshot(hub))
        return output

    def knowledge_step(self, action: str, repeat: bool) -> str:
        if action == "setup" and not repeat:
            self.state["knowledge"] = self.knowledge_runner()
        wiki: Runner = self.state["knowledge"]
        wiki.log = self.log
        return getattr(wiki, f"knowledge_{action}")(repeat)

    def steps(self) -> list[tuple[str, Any]]:
        steps = [("bootstrap", self.bootstrap)]
        steps += [(f"install-cli-{tool}", lambda repeat, tool=tool: self.install_cli(tool, repeat)) for tool in TOOLS]
        steps += [
            ("install-legion", lambda repeat: self.install_repeat() if repeat else self.install_first()),
            ("update-legion", self.update),
            ("setup", lambda repeat: self.setup_repeat() if repeat else self.setup_first()),
            ("add", self.add),
            ("remove", self.remove),
            ("repo-private", lambda repeat: self.repository("private", repeat)),
            ("repo-shared", lambda repeat: self.repository("shared", repeat)),
            ("channel", self.channel),
            ("foundry-launch", self.foundry),
            ("no-pwsh", self.no_pwsh),
            ("uninstall", self.uninstall),
            ("reinstall", self.reinstall),
            ("update-uv-tool", self.update_uv_tool),
            ("purge", self.purge),
        ]
        steps += [(f"knowledge-{action}", lambda repeat, action=action: self.knowledge_step(action, repeat)) for action in ("setup", "uninstall", "declined", "off", "purge")]
        return steps

    def execute(self) -> list[Case]:
        cases = []
        blocked = ""
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        for name, action in self.steps():
            for repeat in (False, True):
                start = time.monotonic()
                self.log = []
                result, message = "passed", ""
                if blocked:
                    result, message = "skipped", blocked
                else:
                    try:
                        if name == "bootstrap":
                            returned = action(repeat)
                        else:
                            with self.without_pwsh():
                                returned = action(repeat)
                        if isinstance(returned, str):
                            logged = "\n".join(self.log).splitlines()
                            self.log.extend(line for line in returned.splitlines() if line not in logged)
                    except Exception as error:
                        result, message = "failed", str(error) or type(error).__name__
                        blocked = f"previous step failed: {name}{' (repeat)' if repeat else ''}"
                log = "\n".join([*self.log, message]).strip()
                case = Case(name, repeat, result, time.monotonic() - start if not blocked or result == "failed" else 0, message, log)
                cases.append(case)
                (self.out / "logs" / f"{name}{'-repeat' if repeat else ''}.log").write_text(log + "\n", encoding="utf-8")
                print(f"{case.title}: {case.result}{': ' + message.splitlines()[0] if message else ''}", flush=True)
        return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--home-root", type=Path)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--allow-source-fallback", action="store_true")
    args = parser.parse_args()
    if os.environ.get("CI") != "true":
        parser.error("integration installs real tools; set CI=true to run with a disposable home")
    if args.home_root is None and not os.environ.get("RUNNER_TEMP"):
        parser.error("--home-root or RUNNER_TEMP is required")
    home = (args.home_root or Path(os.environ["RUNNER_TEMP"]) / "legion-home").resolve()
    system = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    probe = []
    for command in ("winget", "pwsh", "powershell", "node", "git"):
        path = shutil.which(command)
        if path:
            arguments = ["-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"] if command == "powershell" else ["--version"]
            result = subprocess.run([path, *arguments], text=True, capture_output=True, check=False)  # noqa: S603
            probe.append(f"{command}: {result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else 'not found'}")
        else:
            probe.append(f"{command}: not found")
    runner = Runner(args.checkout.resolve(), home, args.out, args.allow_source_fallback)
    with restore_user_path():
        cases = runner.execute()
        extra = ", ".join(f"{tool} {version[0]}" for tool, version in runner.versions.items())
        if runner.not_tested:
            extra += "; not tested from checkout: " + ", ".join(runner.not_tested)
        write_report(args.out, system, cases, probe, extra)
    return int(any(case.result == "failed" for case in cases) or len(runner.not_tested) == len(TOOLS))


if __name__ == "__main__":
    sys.exit(main())
