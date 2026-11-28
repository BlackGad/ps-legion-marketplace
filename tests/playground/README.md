# Lifecycle playground

A local, extra check before pushing at a stop point. An AI agent tries install, update and uninstall like a real owner inside a disposable container and reports what is not smooth. CI on Windows, macOS and Linux stays the required gate. Nothing here touches the owner's real home, PATH or registry, and no token enters an image or container.

Needs Docker Desktop. It runs one mode at a time; the tool says which mode it needs and switches only with `--switch`. Only the committed branch (`HEAD`) is copied in, so commit first.

To stay within the host's memory limit, run one Windows playground container or up to three Linux playground containers at a time. `play.py start` counts containers until `play.py stop` deletes them, including containers that Docker has stopped.

## Run a playground

1. Pick what to run. `python tests/playground/play.py select --base main` lists the intents whose `covers:` paths the diff touches. Run them when lifecycle code changed.
2. `python tests/playground/play.py start linux` (or `windows`, with `--switch` if Docker is in the other mode). The first start builds the image. It prints the container name and the `docker exec` prefix.
3. Hand an agent three things: the intent file (`intents/<name>.md`), the exec prefix, and `report.md`. Tell it to act only through the prefix, stay within the intent's `budget:` minutes and write the filled report. Run Linux intents in parallel within the limit and Windows intents one at a time.
4. `python tests/playground/play.py stop <name>` deletes the container. A failed `start` deletes its own container.
5. Read the report. Switch Docker back to Linux when finished.

In Git Bash on Windows, set `MSYS_NO_PATHCONV=1` before `docker exec -w /work/legion ...`, or the path is rewritten.

## Intent files

One flow per file in `intents/`: front matter with `covers:` (comma separated path globs; `*` also matches `/`), `os:` (`linux`, `windows` or `both`) and `budget:` (minutes), then plain text: the goal, who the agent plays and what to watch. Add a flow by adding a file.

## From a fault to a test

A fault in a report (a hidden question, a silence, a wrong exit code) becomes a deterministic test first: copy the command, the answers and the expected line or exit code from "Suggested fixed tests" into `tests/python/legion/test_lifecycle.py`, watch it fail, then fix.

The tests for `start`, `select` and intent parsing are in `tests/python/test_playground.py`. Pytest never starts a container.
