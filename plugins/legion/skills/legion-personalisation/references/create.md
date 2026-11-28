# Set up, add or remove accounts

1. Start with the `status` tool, as the [read tools guide](../../legion-overseer/references/read-tools.md) describes. It reads settings, accounts, commands and channels. Reuse the owner's earlier answers.

2. Ask which agents to add: the tool, an optional account name, and an existing folder or a new one. Adding no accounts yet is valid. A folder the owner already uses is copied into the new account and keeps its settings and login; the original stays as it is.

3. Show the plan and get the owner's yes. Run with `--yes` if the owner has already approved. Otherwise the command asks `Apply? [y/N]` after showing the plan. A terminal or a pipe can provide the answer, and closed input means no.

```text
legion setup [--yes]
legion add <tool> [<existing folder>] [--id <name>] [--yes]
```

   Setup creates missing settings and notes, installs a missing CLI, adds accounts, installs the legion command with uv and installs legion into every account. A recorded subscription account without its own program starts with the stock CLI on PATH; if that CLI is missing, setup offers to install it. A repeat with no work says `Nothing to do.` Adding an account uses the same `Apply? [y/N]` question, and closed input declines. Finish an earlier setup the same way, with `legion setup`. Owner settings live in `~/.legion/configuration/personalisation.json` and notes in `notes.md` beside it. The same yes runs `uv tool update-shell`, which puts uv's tool folder on PATH by editing the shell profile or, on Windows, the user PATH; the plan and a dry run show it first, and without a yes nothing is edited. If the tool is already installed and its folder is missing from PATH, setup still offers the PATH repair. If the command is unavailable on PATH, follow the instruction on the result line.

4. Read the table's last column. `log in: <command>` is the owner's job: that command starts the tool itself on the account's folder, outside legion, and only the owner runs it and logs in. If the CLI failed to install or needs a new terminal, repeat the add for an unrecorded account once the CLI is available. If the account was added and the plugin step failed, finish setup with `legion setup`. Each failed target has a `failed: <reason>` row and the command exits 1 after the table. Mention once that `legion help` lists the same steps as short commands.

5. Remove an account with `legion remove --id <id>`, rename it with `legion account rename --id <old> --new-id <new>`, and register an existing account's program with `legion account add --id <id> --tool <tool> --command <path>`. Rename and remove update channel bindings, keep the config folder and print its location. Ask before changing or deleting that folder. Foundry uses the tool's account guide. To remove Legion itself, use `legion uninstall`, then read the results table and handover log. Use `legion uninstall --purge` when the owner asks to remove the Legion home after uv succeeds. Purge always asks separately about the knowledge hub. The standard CLI folders can still contain caches managed by their CLIs.

Only Foundry agents ask about models during setup. Do not ask about models for subscription agents, account purposes, worker or reviewer roles, notification timing, channels, or knowledge. Learn preferences later from the owner's words and save them using the [shared memory rule](../SKILL.md#identity-and-hard-rules). Set up a channel only when requested, such as “use Telegram”, through `skills/legion-<kind>/SKILL.md`; stop if missing. The overseer asks about legion access at the [first legion dispatch](../../legion-overseer/references/task-loop.md#first-legion-dispatch). After an account change, move affected repository facts to that repository's workflow and repair prose references in notes; scripts never edit notes. For an alternate home run `legion --home <home> setup`.
