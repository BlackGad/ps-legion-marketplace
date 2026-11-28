# Update commands

Run the forced check:

```text
legion update --check --force
```

The result includes the installed and available versions and the update command, `legion update`. "Installed" is the version of the legion plugin that the `legion` command was installed from. Use the versions in the owner conversation; keep the command internal unless the owner asks for it or must run it themselves. The forced check bypasses the daily session-start cache. Checks read the `stable` branch of `BlackGad/ps-legion-marketplace`, which moves only with a published release. `legion update` follows the track the install was added with; to switch between release and main, uninstall, then install.

Update covers registered accounts and the default folders of stock CLIs on PATH. Status lists these default folders as outlanders. Before asking for approval, read `skills/legion-<tool>/SKILL.md` and its plugin/update reference for each targeted tool. Stop if the tool skill is missing. Explain the documented update scope, including whether it can refresh other marketplaces in a target folder.

## Update your launched overseer session

From the repository, after saving the handoff and notifying the owner, run:

```text
legion update --self --yes
```

On Copilot, add `--session-id <id>` using the current conversation ID from `/session id`. The worker keeps the launcher settings and starts the same conversation after the update. Leave the lease in place and let Legion stop the session.

When the installed command is still 0.23.1, use the new release's copy of the same command:

```text
uvx --isolated --refresh-package legion --from "legion[mcp] @ git+https://github.com/BlackGad/ps-legion-marketplace.git@stable#subdirectory=plugins/legion" legion update --self --yes
```

Read `.legion/temp/self-update.log` on return. Take the lease and report the result on every connected channel. An update failure still leads to a relaunch when the old session has stopped. Worker diagnostics are in `<home>/.legion/temp/self-update.err`. If the command prints `skip: self-update (already current)`, report it and continue in the current session. If the request is refused, report the reason and any listed process IDs in the current session. If a stop times out and the overseer remains up, read the result there. When you recover from a relaunch failure, open Legion in the same repository and read the handoff and result.

## Plain owner update

On Windows, after the owner's yes, tell them to close the Legion session, run `legion update` from a terminal, then run `legion` in the repository to reopen the conversation. The update refuses before making changes if it finds a registered ancestor. The owner runs the update themselves.

On macOS and Linux, after the owner's yes, run `legion update --yes`. The flag skips the single `Apply? [y/N]` question for the whole plan, including missing plugin copies. Restart the starting session afterwards to load the new skills.

If the check fails, explain the failed check and offer the GitHub update from the README. It still requires the owner's yes. On Windows, give the owner the PowerShell command to run in a terminal after closing the Legion session.

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/BlackGad/ps-legion-marketplace/main/install.ps1))) -Update
```

The bootstrap announces preparation, then runs `legion update` through uv. Use `--update` after `sh -s --` on macOS and Linux. The update shows the plan and asks for confirmation in the terminal. If the owner declines, the prompt to retry names `-Yes`, the PowerShell bootstrap flag.

`legion update` waits for MCP servers and `legion run` wrappers registered under its own legion home to exit before replacing the command. Tell the owner before approval. If legion reports that other sessions are still running after 30 seconds, ask the owner to close them and run it again. Continue stopped members from the [handoff](../../legion-overseer/references/takeover.md).

The bootstrap and the installed command on macOS and Linux display uv's progress and the result in the terminal. A failed foreground uv install exits with status 1. On Windows, the installed command can schedule its own replacement after it exits. The terminal shows the log path and asks the owner to restart after replacement succeeds. Check `<home>/.legion/temp/uv-tool.log` for the uv exit code or a handover error before reporting success. `legion status` flags a failed handover and points to the log. An update with no work exits without a question. If the owner selects a checkout with `--source`, the plan names that checkout and applies content changes at the same version. A failed target has a `failed: <reason>` result row. Other targets continue, and uv uses the newest successful copy. Read the final result and the table before reporting success.
