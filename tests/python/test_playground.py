import importlib.util
import sys
from pathlib import Path

import pytest


PLAY = Path(__file__).resolve().parents[1] / "playground" / "play.py"
SPEC = importlib.util.spec_from_file_location("playground_play", PLAY)
assert SPEC
assert SPEC.loader
play = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(play)

INTENT = """---
covers: plugins/legion/src/legion/services/lifecycle/*, install.sh
os: linux
budget: 15
---
Goal: install.
"""


def write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(text, encoding="utf-8")


def test_parse_intent_reads_front_matter_and_body() -> None:
    intent = play.parse_intent("install", INTENT)
    assert intent.name == "install"
    assert intent.covers == ["plugins/legion/src/legion/services/lifecycle/*", "install.sh"]
    assert intent.os == "linux"
    assert intent.budget == 15
    assert intent.text.strip() == "Goal: install."


def test_parse_intent_rejects_missing_front_matter() -> None:
    with pytest.raises(ValueError, match="front matter"):
        play.parse_intent("bad", "Goal only.")


def test_parse_intent_rejects_unknown_os() -> None:
    with pytest.raises(ValueError, match="os"):
        play.parse_intent("bad", INTENT.replace("os: linux", "os: beos"))


def test_select_lists_only_playgrounds_whose_paths_changed(tmp_path: Path) -> None:
    write(tmp_path, "a.md", INTENT)
    write(tmp_path, "b.md", INTENT.replace("covers: plugins/legion/src/legion/services/lifecycle/*, install.sh", "covers: install.ps1"))
    names = [intent.name for intent in play.select(["plugins/legion/src/legion/services/lifecycle/apply.py"], tmp_path)]
    assert names == ["a"]
    assert play.select(["README.md"], tmp_path) == []


def test_shipped_intents_parse_and_cover_something() -> None:
    intents = play.load_intents(PLAY.parent / "intents")
    assert len(intents) >= 3
    assert all(intent.covers and intent.budget > 0 for intent in intents)


@pytest.mark.parametrize(("os_name", "count", "message"), [("windows", 1, "one Windows playground container"), ("linux", 3, "three Linux playground containers")])
def test_start_refuses_when_the_container_limit_is_reached(os_name: str, count: int, message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def docker(*args: str, **_options: bool) -> str:
        if args[0] == "version":
            return os_name
        if args[0] == "ps":
            return "\n".join(f"legion-play-{os_name}-{index}" for index in range(1, count + 1))
        pytest.fail(f"start tried to {args[0]} a container at the limit")

    monkeypatch.setattr(play, "docker", docker)
    monkeypatch.setattr(sys, "argv", [str(PLAY), "start", os_name])
    with pytest.raises(SystemExit, match=message):
        play.main()
