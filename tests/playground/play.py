"""Disposable containers for the lifecycle playground: start, stop and select (see README.md)."""

import argparse
import fnmatch
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OS_NAMES = ("linux", "windows")
IMAGES = {name: f"legion-playground:{name}" for name in OS_NAMES}
WORK = {"linux": "/work", "windows": "C:\\work"}
KEEP_ALIVE = {"linux": ["sleep", "infinity"], "windows": ["ping", "-t", "localhost"]}
SWITCH_FLAG = {"linux": "-SwitchLinuxEngine", "windows": "-SwitchWindowsEngine"}
CONTAINER_LIMIT = {"linux": 3, "windows": 1}


@dataclass
class Intent:
    name: str
    covers: list[str]
    os: str
    budget: int
    text: str


def parse_intent(name: str, content: str) -> Intent:
    """Split an intent file into its front matter (covers, os, budget) and its plain text."""
    parts = content.split("---\n", 2)
    if parts[0].strip() or len(parts) < 3:
        raise ValueError(f"{name}: the file must start with a front matter block between --- lines")
    fields = {}
    for line in parts[1].splitlines():
        if line.strip():
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    covers = [path.strip() for path in fields.get("covers", "").split(",") if path.strip()]
    if not covers:
        raise ValueError(f"{name}: covers lists no path")
    os_name = fields.get("os", "")
    if os_name not in (*OS_NAMES, "both"):
        raise ValueError(f"{name}: os must be linux, windows or both, not '{os_name}'")
    if not fields.get("budget", "").isdigit():
        raise ValueError(f"{name}: budget must be a number of minutes")
    return Intent(name, covers, os_name, int(fields["budget"]), parts[2])


def load_intents(folder: Path) -> list[Intent]:
    return [parse_intent(path.stem, path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.md"))]


def select(changed: list[str], folder: Path) -> list[Intent]:
    """Return the intents that cover at least one changed path."""
    return [intent for intent in load_intents(folder) if any(fnmatch.fnmatch(path, glob) for path in changed for glob in intent.covers)]


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)  # noqa: S603, S607
    if check and result.returncode != 0:
        raise SystemExit(f"docker {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def docker_mode() -> str:
    return docker("version", "--format", "{{.Server.Os}}", check=False)


def ensure_mode(os_name: str, switch: bool) -> None:
    """Stop with a plain message when Docker is in the other mode; switch only when asked."""
    if docker_mode() == os_name:
        return
    if not switch:
        raise SystemExit(f"Docker is not in {os_name} mode. Run again with --switch to switch it.")
    cli = Path(os.environ.get("PROGRAMFILES", "")) / "Docker" / "Docker" / "DockerCli.exe"
    if not cli.exists():
        raise SystemExit(f"Cannot switch automatically: {cli} was not found. Switch Docker Desktop to {os_name} containers by hand.")
    subprocess.run([str(cli), SWITCH_FLAG[os_name]], stdin=subprocess.DEVNULL, check=True)  # noqa: S603
    deadline = time.monotonic() + 180
    while docker_mode() != os_name:
        if time.monotonic() > deadline:
            raise SystemExit(f"Docker did not reach {os_name} mode within 3 minutes.")
        time.sleep(3)
    print(f"Docker switched to {os_name} mode.")


def build_image(os_name: str) -> None:
    if docker("image", "inspect", IMAGES[os_name], "--format", "{{.Id}}", check=False):
        return
    print(f"Building {IMAGES[os_name]} (first time only)...", flush=True)
    docker("build", "-t", IMAGES[os_name], "-f", str(HERE / f"Dockerfile.{os_name}"), str(HERE))


def copy_branch(name: str, os_name: str) -> None:
    """Copy the committed branch into the container as a tar stream; nothing is mounted."""
    git = shutil.which("git") or "git"
    archive = subprocess.Popen([git, "archive", "--format=tar", "--prefix=legion/", "HEAD"], cwd=ROOT, stdout=subprocess.PIPE)  # noqa: S603
    copy = subprocess.run(["docker", "cp", "-", f"{name}:{WORK[os_name]}"], stdin=archive.stdout, capture_output=True, check=False)  # noqa: S603, S607
    if archive.stdout:
        archive.stdout.close()
    if archive.wait() != 0 or copy.returncode != 0:
        raise SystemExit(f"Copying the branch into the container failed:\n{copy.stderr.decode(errors='replace').strip()}")


def start(os_name: str, switch: bool) -> None:
    ensure_mode(os_name, switch)
    prefix = f"legion-play-{os_name}-"
    containers = set(docker("ps", "--all", "--filter", f"name=^/{prefix}", "--format", "{{.Names}}").splitlines())
    limit = CONTAINER_LIMIT[os_name]
    if len(containers) >= limit:
        maximum = "one Windows playground container" if os_name == "windows" else "three Linux playground containers"
        raise SystemExit(f"Run at most {maximum} at a time. Stop a playground container before starting another.")
    build_image(os_name)
    # Docker reserves each slot name so concurrent starts cannot exceed the limit.
    name = next(f"{prefix}{slot}" for slot in range(1, limit + 1) if f"{prefix}{slot}" not in containers)
    # Hyper-V containers cannot be copied into while running.
    docker("create", "--name", name, IMAGES[os_name], *KEEP_ALIVE[os_name])
    try:
        copy_branch(name, os_name)
        docker("start", name)
    except BaseException:
        docker("rm", "-f", name, check=False)
        raise
    folder = WORK[os_name] + ("/" if os_name == "linux" else "\\") + "legion"
    shell = "bash -lc" if os_name == "linux" else "powershell -NoProfile -Command"
    print(f"container: {name}")
    print(f"exec prefix: docker exec -w {folder} {name}")
    print(f'example: docker exec -w {folder} {name} {shell} "uv --version"')
    print(f"stop: python tests/playground/play.py stop {name}")


def stop(name: str, switch: bool) -> None:
    os_name = name.split("-")[2] if name.count("-") == 3 else ""
    if os_name not in OS_NAMES:
        raise SystemExit(f"'{name}' is not a playground container name.")
    ensure_mode(os_name, switch)
    docker("rm", "-f", name)
    print(f"deleted: {name}")


def changed_paths(base: str) -> list[str]:
    git = shutil.which("git") or "git"
    result = subprocess.run([git, "diff", "--name-only", base], cwd=ROOT, capture_output=True, text=True, check=True)  # noqa: S603
    return result.stdout.splitlines()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("os", choices=OS_NAMES)
    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("name")
    for sub in (start_parser, stop_parser):
        sub.add_argument("--switch", action="store_true", help="switch Docker Desktop to the needed mode")
    commands.add_parser("select").add_argument("--base", required=True)
    args = parser.parse_args()
    if args.command == "start":
        start(args.os, args.switch)
    elif args.command == "stop":
        stop(args.name, args.switch)
    else:
        for intent in select(changed_paths(args.base), HERE / "intents"):
            print(f"{intent.name}\tos={intent.os}\tbudget={intent.budget}m\t{HERE / 'intents' / (intent.name + '.md')}")


if __name__ == "__main__":
    main()
