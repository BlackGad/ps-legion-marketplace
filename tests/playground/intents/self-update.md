---
covers: plugins/legion/src/legion/cli/*, plugins/legion/src/legion/core/_stop_event.py, plugins/legion/src/legion/services/accounts/*, plugins/legion/src/legion/services/channels/_overseer.py, plugins/legion/src/legion/services/installation/*, plugins/legion/src/legion/services/lifecycle/*
os: both
budget: 15
---

Goal: bring back a launched overseer automatically after updating Legion to the checkout.

Use a disposable container with no logins or mounted credentials. Install the actual 0.23.1 release as a uv tool. Put a fake Claude CLI on PATH and register a disposable account and Telegram binding. The fake CLI can use `tests/python/legion/fake_cli.py` with `FAKE_OVERSEER_REQUEST` set to the checkout and `FAKE_OVERSEER_UVX=1`. Keep the stock CLIs off PATH so the checkout update reaches only fakes.

1. Set up a scratch repository with a handoff. Start the fake overseer through the installed launcher with a literal argument and a channel. Check that 0.23.1 has no self-update option.
2. Let the fake session take the lease and invoke the self-update via uvx from the checkout. Use real uv for the command replacement. The 0.23.1 launcher can report its planned stop as a failure with exit code 143 on Linux or 15 on Windows. Check the result file and check that the installed command now offers self-update.
3. Compare the two launches. The account folder, repository, channel folder and literal arguments match, and the new invocation uses the original conversation ID with the CLI's resume argument. Read the repository result and handoff.
4. While the resumed overseer is still running, bump the version in the disposable checkout's plugin manifest to simulate a second update. Run the installed `legion update --self --yes` without a source flag. Check that the first resumed process stops normally, the new version installs and the same conversation resumes again.
5. Repeat against the now-unchanged checkout and check for `skip: self-update (already current)`. Verify that the current overseer's PID and the result file stay the same. Stop the container when finished.

Report the commands, timing, result line, and any friction or failures. Never try this in an owner session on the host.
