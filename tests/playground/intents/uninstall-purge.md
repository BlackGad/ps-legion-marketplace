---
covers: plugins/legion/src/legion/services/lifecycle/*, plugins/legion/src/legion/services/installation/*, plugins/legion/src/legion/core/_home.py, plugins/legion/src/legion/core/_process.py, plugins/legion/src/legion/cli/*
os: both
budget: 20
---
Goal: remove legion in two steps, a plain uninstall and then a purge, and see what each leaves.

You play: an owner who has used legion for a while and now wants it gone. First install legion from the checkout and add one account, so there is something to remove (do not log in).

Do:
1. `legion uninstall --dry-run`, then answer `n` to the real question. Nothing may change.
2. `legion uninstall`, answer yes. Check what stays: `~/.legion` settings, notes and account folders must remain; the legion command must be gone.
3. Install again (uninstall then install is an upgrade) and check the settings survived.
4. `legion uninstall --purge`. Answer the knowledge hub question if it appears. Check `~/.legion` is gone and the CLIs are still installed.
5. From the checkout, repeat through the bootstrap with `sh install.sh --uninstall` on Linux or `.\install.ps1 -Uninstall` on Windows. It must say there is nothing to do.

Watch for:
- Every question is visible before it waits for input; the purge says what it deletes before asking.
- Progress during the removal steps and during the wait for running sessions.
- One plain result line, and an exit code of 0 for done, declined and already removed.
- List `~/.legion`, the uv tool folder and the CLI folders after each step. The stock CLIs may keep their plugin caches after purge.
- Time every step.
