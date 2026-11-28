# Record uv tool changes without touching an interpreter or shell profile.

import json
import os
import shutil
import sys
from pathlib import Path

from fake_cli import log_call, write_shim


def main(args: list[str]) -> int:
    state_file = Path(os.environ["FAKE_UV_STATE"])
    log_call(state_file.with_suffix(".log"), args)
    group, *operands = args
    if group == "tool":
        verb, *operands = operands
    elif group == "export":
        verb = group
    else:
        raise ValueError(f"unexpected uv arguments: {args}")
    if os.environ.get("FAKE_UV_FAIL") == verb:
        if verb == "install" and os.environ.get("FAKE_OVERSEER_REQUEST"):
            tool = Path(os.environ["UV_TOOL_DIR"]) / "legion"
            scratch = Path(os.environ["LEGION_HOME"]) / ".legion/temp"
            locations = [str(path.parents[2]) for parent in (tool.parent, scratch) for path in parent.glob("legion-recovery-*/environment/installed")]
            state_file.with_suffix(".recovery").write_text(json.dumps(locations), encoding="utf-8")
            shutil.rmtree(tool)
            bin_dir = Path(os.environ["UV_TOOL_BIN_DIR"])
            (bin_dir / ("legion.cmd" if sys.platform == "win32" else "legion")).unlink()
        print(f"fake uv: {verb} failed", file=sys.stderr)
        return 1

    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    if verb == "list":
        if "version" in state:
            print(f"legion v{state['version']}\n- legion")
    elif verb == "dir":
        print(os.environ["UV_TOOL_BIN_DIR" if operands == ["--bin"] else "UV_TOOL_DIR"])
    elif verb == "export":
        assert (Path.cwd() / "plugin.json").is_file()
        print("fake-dependency==1.0.0")
    elif verb == "install":
        if os.environ.get("FAKE_OVERSEER_REQUEST"):
            assert not list((Path(os.environ["LEGION_HOME"]) / ".legion/run").glob("*.pid"))
        spec, flag, constraints, reinstall = operands
        assert spec.endswith("[mcp]")
        assert (flag, reinstall) == ("--constraints", "--reinstall")
        assert "fake-dependency==1.0.0" in Path(constraints).read_text(encoding="utf-8")
        root = Path(spec.removesuffix("[mcp]"))
        version = json.loads((root / "plugin.json").read_text(encoding="utf-8"))["version"]
        state_file.write_text(json.dumps({"version": version, "source": str(root)}), encoding="utf-8")
        write_shim(Path(os.environ["UV_TOOL_BIN_DIR"]), "legion", Path(__file__))
    elif verb == "uninstall":
        assert operands == ["legion"]
        state_file.unlink()
        bin_dir = Path(os.environ["UV_TOOL_BIN_DIR"])
        (bin_dir / ("legion.cmd" if sys.platform == "win32" else "legion")).unlink(missing_ok=True)
    elif verb == "update-shell":
        if sys.platform == "win32":
            Path(os.environ["LEGION_TEST_USER_PATH_FILE"]).write_text(os.environ["UV_TOOL_BIN_DIR"], encoding="utf-8")
    else:
        raise ValueError(f"unexpected uv arguments: {args}")
    return 0


if __name__ == "__main__":
    assert sys.argv[1] == "uv"
    raise SystemExit(main(sys.argv[2:]))
