import json
from pathlib import Path

import pytest
from cli_runner import run
from launcher_fakes import fake_cli, only_on_path

CREDENTIALS = b'{"token": "keep me exactly"}\n'


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, variable in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("copilot", "COPILOT_HOME")):
        fake_cli(tmp_path / "tools", name, variable)
    only_on_path(monkeypatch, tmp_path / "tools")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "home"


def add(home: Path, tool: str, source: Path, account: str) -> int:
    return run(["--home", str(home), "account", "add", "--id", account, "--tool", tool, "--config-dir", str(source)])


def spellings(source: Path) -> str:
    plain = str(source)
    escaped = json.dumps(plain)[1:-1]
    return "\n".join([plain, escaped, source.as_posix(), plain + "-account2", plain + ".bak", "\\?\\" + plain])


@pytest.mark.parametrize(
    ("tool", "listed"),
    [
        ("claude", ["plugins/installed_plugins.json", "plugins/known_marketplaces.json"]),
        ("codex", ["config.toml", ".codex-global-state.json"]),
        ("copilot", ["config.json"]),
    ],
)
def test_adopt_copies_the_folder_and_rewrites_only_the_listed_files(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], tool: str, listed: list[str]) -> None:
    source = tmp_path / f".{tool}"
    target = home / ".legion" / "agents" / "acct"
    for name in [*listed, "history.json"]:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(spellings(source), encoding="utf-8")
    (source / ".credentials.json").write_bytes(CREDENTIALS)
    before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}

    assert add(home, tool, source, "acct") == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"copying {source} into {target}"
    assert out[-1] == f"Copied {source} into {target}; open it with 'legion --account acct'."
    assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before
    assert (target / ".credentials.json").read_bytes() == CREDENTIALS
    assert (target / "history.json").read_text(encoding="utf-8") == spellings(source)
    for name in listed:
        text = (target / name).read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == str(target)
        assert lines[1] == json.dumps(str(target))[1:-1]
        assert lines[2] == target.as_posix()
        assert lines[3] == str(source) + "-account2"
        assert lines[4] == str(source) + ".bak"
        assert lines[5] == "\\?\\" + str(target)
        assert f"rewrote 4 path(s) in {target / name}" in out


def test_a_folder_that_fails_to_copy_leaves_no_account_and_the_source_stays(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / ".claude"
    (source / "plugins").mkdir(parents=True)
    (source / "plugins" / "installed_plugins.json").write_bytes(b"\xff\xfe not utf-8")
    assert add(home, "claude", source, "acct") == 1
    assert not (home / ".legion" / "agents" / "acct").exists()
    assert not (home / ".legion" / "configuration" / "personalisation.json").exists() or "acct" not in (home / ".legion" / "configuration" / "personalisation.json").read_text(encoding="utf-8")
    assert capsys.readouterr().err.strip() != ""
    assert (source / "plugins" / "installed_plugins.json").read_bytes() == b"\xff\xfe not utf-8"


def test_a_target_that_already_has_files_is_refused(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / ".codex"
    source.mkdir()
    target = home / ".legion" / "agents" / "acct"
    target.mkdir(parents=True)
    (target / "mine.txt").write_text("mine", encoding="utf-8")
    assert add(home, "codex", source, "acct") == 1
    assert "already has files" in capsys.readouterr().err
    assert (target / "mine.txt").read_text(encoding="utf-8") == "mine"
