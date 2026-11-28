# uv installs Legion from the plugin copy, using its version and locked dependencies.

import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Optional

from legion.core import _settings_io, _stop_event
from legion.core._home import (
    owner_path_safe,
    path_contains,
    resolve_directory,
    same_path,
    settings_path,
)
from legion.core._run import (
    display_command,
    launch_environment,
    run_command,
    run_foreground,
)
from legion.core._settings_models import PluginManifest
from legion.services.installation import _handover, _user_path
from legion.services.installation._constraints import export_constraints

UV_MISSING = "uv is missing. Ask the owner before installing it, then run the legion installer again."
TOOL = "legion"
STOP_NOTICE = "stop     running legion sessions (mcp servers and runs) end first"
HANDOVER_LOG = Path(".legion/temp/uv-tool.log")
SOURCE_RECEIPT = Path(".legion/uv-source")


class UvToolError(ValueError):
    pass


def find_uv() -> Optional[str]:
    return shutil.which("uv")


def plugin_version(plugin_root: Path) -> str:
    return PluginManifest.model_validate_json((plugin_root / "plugin.json").read_text(encoding="utf-8-sig")).version


# The version of the installed legion tool, or None when uv lists none.
def installed_version(uv: str) -> Optional[str]:
    result = run_command(uv, ["tool", "list"])
    if result.exit_code != 0:
        return None
    for line in result.output.splitlines():
        words = line.split()
        if len(words) > 1 and words[0] == TOOL and words[1].startswith("v"):
            return words[1].removeprefix("v")
    return None


def bin_directory(uv: str) -> Optional[Path]:
    result = run_command(uv, ["tool", "dir", "--bin"])
    return Path(result.output.strip()) if result.exit_code == 0 and result.output.strip() else None


def path_missing(uv: str) -> bool:
    folder = bin_directory(uv)
    return folder is not None and not path_contains(_saved_path(), folder)


def _saved_path() -> str:
    if sys.platform == "win32":
        return os.pathsep.join(_user_path.persistent_folders())
    return os.environ.get("PATH", "")


# The lock file's exact versions, written into a folder of its own, which the caller removes.
def _write_constraints(uv: str, plugin_root: Path) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="legion-constraints-"))
    try:
        (folder / "constraints.txt").write_text(export_constraints(plugin_root, uv), encoding="utf-8")
    except (RuntimeError, OSError) as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise UvToolError(str(error)) from error
    return folder


# True when this process runs from the legion tool itself, whose files uv is about to replace.
def running_from_tool(uv: str) -> bool:
    result = run_command(uv, ["tool", "dir"])
    return result.exit_code == 0 and bool(result.output.strip()) and same_path(resolve_directory(Path(sys.prefix)), resolve_directory(Path(result.output.strip()) / TOOL))


# Waits for registered processes that can be stopped. On a timeout, lift the stop event before reporting the failure.
def stop_running(home: Path, *, updating: bool = False) -> None:
    if not owner_path_safe(_stop_event.stop_path(home), home):
        raise UvToolError("The legion stop file path contains a link; resolve it before continuing")
    remaining = _stop_event.stop_all(home, updating=updating)
    if remaining:
        _stop_event.clear_stop(home)
        ids = ", ".join(str(pid) for pid in remaining)
        raise UvToolError(f"legion is still running after {_stop_event.WAIT_SECONDS:g} seconds (process {ids}); close those sessions and run the command again")


def _hand_over(home: Path, command: list[str], cleanup: list[Path], purge: list[Path], receipt: tuple[Path, str] | None = None) -> None:
    log = home / HANDOVER_LOG
    if not owner_path_safe(log, home):
        if command:
            raise UvToolError("The uv tool log path contains a link; resolve it before continuing")
        # A cleanup-only purge can remove the link without writing through it.
        _handover.start(command, cleanup, purge=purge, log=None)
        return
    if purge and log.parent not in purge and log.parent.parent not in purge:
        # Keeping the hub can leave a new log directory outside the original purge list.
        purge = [*purge, log.parent]
    if receipt:
        _handover.start(command, cleanup, purge=purge, log=log, receipt=receipt)
    else:
        _handover.start(command, cleanup, purge=purge, log=log)


def handover_failure(home: Path) -> str | None:
    log = home / HANDOVER_LOG
    if not owner_path_safe(log, home):
        return None
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 4096))
            if any(line.startswith(_handover.FAILURE_PREFIX.encode("ascii")) for line in stream.read(4096).splitlines()):
                return f"Legion handover failed. See {log} for details."
    except OSError:
        pass
    return None


def _reachable() -> bool:
    return shutil.which(TOOL, path=launch_environment().get("PATH", "")) is not None or (sys.platform == "win32" and shutil.which(TOOL, path=_saved_path()) is not None)


def path_repair_needed(uv: str) -> bool:
    return not _reachable() and path_missing(uv)


def _path_result(uv: str, *, update_shell: bool) -> str | None:
    if update_shell and path_missing(uv):
        run_command(uv, ["tool", "update-shell"])
    if _reachable():
        return None
    folder = bin_directory(uv)
    return f"Open a new terminal. If legion is still unavailable, add {folder or "uv's tool folder"} to your PATH."


def replacement_needed(uv: str, root: Path, home: Path, receipt: str | None) -> bool:
    if installed_version(uv) != plugin_version(root):
        return True
    if receipt is None:
        return False
    path = home / SOURCE_RECEIPT
    return not owner_path_safe(path, home) or not path.is_file() or path.read_text(encoding="utf-8") != receipt


def _check_install_home(home: Path) -> None:
    if not owner_path_safe(home / ".legion", home):
        raise _settings_io.SettingsError("The legion folder contains a link; resolve it before installing")
    with _settings_io.writer(home, require_existing=False):
        if settings_path(home).is_file():
            _settings_io.load(home)


# Replaces legion with the plugin at `plugin_root`. A repeat only repairs PATH if needed and approved.
# The stop event precedes replacement. On Windows a helper waits for the running tool to exit
# before replacing it. `update_shell` also runs `uv tool update-shell`,
# which edits the owner's shell profile or user PATH: callers pass it only inside the owner's yes.
def install(
    plugin_root: Path,
    home: Path,
    dry_run: bool = False,
    update_shell: bool = False,
    *,
    updating: bool = False,
    progress: Callable[[str], None] | None = None,
    receipt: str | None = None,
    rollback: bool = False,
) -> list[str]:
    if not dry_run:
        _check_install_home(home)
    uv = find_uv()
    if uv is None:
        if dry_run:
            return [UV_MISSING]
        raise UvToolError(UV_MISSING)
    version = plugin_version(plugin_root)
    if not replacement_needed(uv, plugin_root, home, receipt):
        if update_shell and path_repair_needed(uv):
            if dry_run:
                return [display_command([uv, "tool", "update-shell"])]
            instruction = _path_result(uv, update_shell=True)
            return [f"legion command {version} is installed but not on PATH in this terminal. {instruction}" if instruction else f"legion command {version} is on PATH."]
        return [f"skip: legion command {version} (already done)"]
    spec = f"{plugin_root}[mcp]"
    if dry_run:
        shell = [display_command([uv, "tool", "update-shell"])] if update_shell else []
        return [STOP_NOTICE, display_command([uv, "tool", "install", spec, "--constraints", "<exported uv.lock>", "--reinstall"]), *shell]
    scheduled = _replace(uv, spec, home, plugin_root, version, updating, update_shell, progress, receipt, rollback)
    if scheduled:
        return [scheduled]
    outcome = f"legion {version} is installed; restart your sessions." if updating else f"legion command {version} installed"
    instruction = _path_result(uv, update_shell=update_shell)
    return [f"legion command {version} is installed but not on PATH in this terminal. {instruction}" if instruction else outcome]


def _replace(
    uv: str, spec: str, home: Path, plugin_root: Path, version: str, updating: bool, update_shell: bool, progress: Callable[[str], None] | None, receipt: str | None, rollback: bool
) -> str | None:
    detached = sys.platform == "win32" and running_from_tool(uv)
    if detached and rollback:
        raise UvToolError("Self-update requires a separate uv environment")
    if progress is not None:
        progress("Waiting for running Legion sessions to exit...")
    stop_running(home, updating=updating)
    folder: Optional[Path] = None
    handed_over = False
    try:
        if progress is not None:
            progress(f"Preparing locked dependencies for legion {version}...")
        folder = _write_constraints(uv, plugin_root)
        command = [uv, "tool", "install", spec, "--constraints", str(folder / "constraints.txt"), "--reinstall"]
        source_file = home / SOURCE_RECEIPT
        if not owner_path_safe(source_file, home):
            raise UvToolError("The uv source receipt path contains a link; resolve it before installing")
        if detached:
            source_file.unlink(missing_ok=True)
            if update_shell and path_missing(uv):
                run_command(uv, ["tool", "update-shell"])
            cleanup = [folder, _stop_event.stop_path(home)]
            if receipt:
                _hand_over(home, command, cleanup, [], (source_file, receipt))
            else:
                _hand_over(home, command, cleanup, [])
            handed_over = True
            scheduled = f"legion command {version} replacement scheduled. Check the result in {home / HANDOVER_LOG}."
            if updating:
                scheduled += " Restart your sessions after the replacement succeeds."
            return scheduled
        if progress is not None:
            progress(f"Installing legion {version} with uv...")
        with _restore_failed_replacement(uv, home, source_file) if rollback else nullcontext():
            source_file.unlink(missing_ok=True)
            exit_code = _run_install(uv, command[1:], progress)
            if exit_code:
                raise UvToolError(f"uv tool install exited {exit_code}")
            if receipt is not None:
                source_file.write_text(receipt, encoding="utf-8")
    finally:
        if not handed_over:
            _stop_event.clear_stop(home)
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
    return None


@contextmanager
def _restore_failed_replacement(uv: str, home: Path, receipt: Path) -> Iterator[None]:
    # uv can remove the old tool and its entry point before an install fails.
    directory = run_command(uv, ["tool", "dir"])
    binaries = bin_directory(uv)
    entry = shutil.which(TOOL, path=str(binaries)) if binaries else None
    if directory.exit_code or not directory.output.strip() or entry is None:
        raise UvToolError("Could not locate the installed Legion command for recovery")
    tool = Path(directory.output.strip()) / TOOL
    launcher = Path(entry)
    paths = (tool, launcher, receipt)
    temp = home / ".legion/temp"
    if not owner_path_safe(temp, home):
        raise UvToolError("The Legion recovery folder contains a link; resolve it before updating")
    temp.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="legion-recovery-", dir=temp))
    restoring = False
    try:
        copies = [scratch / name for name in ("environment", "entry", "receipt")]
        for path, copy in zip(paths, copies, strict=True):
            if path.is_dir() and not path.is_symlink():
                shutil.copytree(path, copy, symlinks=True)
            elif os.path.lexists(path):
                shutil.copy2(path, copy, follow_symlinks=False)
        try:
            yield
        except BaseException:
            restoring = True
            for path, copy in zip(paths, copies, strict=True):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                if os.path.lexists(copy):
                    shutil.move(str(copy), str(path))
            restoring = False
            raise
    finally:
        if not restoring:
            shutil.rmtree(scratch, ignore_errors=True)


def _run_install(uv: str, arguments: list[str], progress: Callable[[str], None] | None) -> int:
    if progress is None:
        return run_foreground(uv, arguments)

    def report(line: str) -> None:
        if not (line.startswith("warning:") and "is not on your PATH" in line):
            progress(line)

    return run_command(uv, arguments, progress=report).exit_code


# Remove the tool and any approved folders after exit. The helper also lifts the stop event.
def uninstall(home: Path, dry_run: bool = False, *, stopped: bool = False, purge: list[Path] | None = None, progress: Callable[[str], None] | None = None) -> list[str]:
    uv = find_uv()
    command = [uv, "tool", "uninstall", TOOL] if uv is not None and installed_version(uv) is not None else []
    purge = purge or []
    if not (command or purge):
        return []
    if dry_run:
        return [STOP_NOTICE, display_command(command)] if command else []
    if command and not stopped:
        stop_running(home)
        stopped = True
    if sys.platform != "win32":
        try:
            if command:
                if progress:
                    progress("Removing the legion command with uv...")
                result = run_command(command[0], command[1:], progress=progress)
                if result.exit_code:
                    raise UvToolError(f"uv tool uninstall exited {result.exit_code}")
                (home / SOURCE_RECEIPT).unlink(missing_ok=True)
            for path in purge:
                if progress:
                    progress(f"Removing {path}...")
                _handover.purge_folder(path)
        finally:
            if stopped:
                _stop_event.clear_stop(home)
        return ["legion command removed and Legion data purged." if command and purge else "legion command removed." if command else "Legion data purged."]

    try:
        _hand_over(home, command, [_stop_event.stop_path(home)] if stopped else [], purge)
    except BaseException:
        if stopped:
            _stop_event.clear_stop(home)
        raise
    if purge:
        return ["legion command removal scheduled, followed by Legion data removal." if command else "Legion folder removal scheduled."]
    return [f"legion command removal scheduled. Check the result in {home / HANDOVER_LOG}."]
