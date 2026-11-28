# Technical reference

Installer options, CLI guides and repository details for maintainers and power users.

The Windows and POSIX bootstraps select `legion install`, `legion update` or `legion uninstall` from their options. [Installer parameters](#installer-parameters) maps the spellings.

## Requirements

- [uv](https://docs.astral.sh/uv/) on every machine. The bootstrap installs it after asking when it is missing; legion itself runs in Python through uv, and uv downloads the Python it needs.
- On Windows: the built-in Windows PowerShell 5.1 (or PowerShell 7) to start the bootstrap.
- On macOS and Linux: `sh`, `curl` and `tar`.
- At least one of: Claude Code, Codex CLI, Copilot CLI.

## Install

The install one-liners are in the [README](../README.md#install). The bootstrap script comes from the `main` branch, but it downloads the legion code archive from the `stable` branch (the latest release) unless you ask for `main` (`-Ref main`, `--main`). The archive supplies the Legion command. The version legion reports is read from the `plugin.json` of the plugin installed in each tool, not from the archive: if an older plugin is already installed and enabled, the installer can skip it, so a newer bootstrap run over it may still report the older version until you run an update. A local checkout uses its own files instead.

The bootstrap obtains uv and runs the selected Legion command. On Windows it runs in Windows PowerShell 5.1 or PowerShell 7. If uv is missing, it says so and asks `Install uv now? [y/N]`; after a yes it reads the latest uv release from GitHub, downloads the archive for your system, checks it against the release's SHA-256 digest (a mismatch deletes the download and stops) and publishes `uv` to uv's default folder (`~/.local/bin`, or `UV_INSTALL_DIR` / `XDG_BIN_HOME`; extracted privately first, run once to check it works, then renamed into place). uv is a global helper like git: legion never removes it. POSIX needs no sudo. Every download must be HTTPS, redirects included. If Python 3.12 is missing, `-DryRun` (`--dry-run`) only says so; an ordinary run asks before uv downloads it.

The bootstrap announces preparation and runs the selected verb through `uv run` in a temporary environment. It uses the local checkout if the script runs from one and downloads the archive from the fixed Legion repository otherwise. The plan names the marketplace source, asks `Apply? [y/N]` when there is work, and treats closed input as no. The bootstrap can also ask for missing prerequisites. Use `-Yes` for the PowerShell bootstrap, `sh -s -- --yes` for the piped POSIX bootstrap, or `--yes` for the Legion CLI to approve the plan without the question. For a POSIX bootstrap with a directly supplied yes or no, run the script from a file so its stdin can carry the answer.

One `uv tool install "<plugin folder>[mcp]" --constraints <exported uv.lock> --reinstall` installs the command from the newest successful plugin copy or from the explicitly selected checkout. Install and setup can run `uv tool update-shell` with the same approval. This edits the shell profile or the Windows user PATH. When Legion is unavailable on PATH, the result gives the uv tool folder to add. Update leaves PATH alone.

By default, the script installs legion for every installed coding CLI, using the targets below. If none is installed, it says so and prints each CLI's install hint; add an account with `legion add <tool>`, which installs the CLI. Repeated runs skip completed steps and preserve hook approval.

### Install and update targets

Install, update and uninstall use the same target discovery.

- Each CLI has one outlander folder when its stock command is on PATH. The folders are `~/.claude`, `~/.codex`, and `~/.copilot`. Target discovery ignores `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and `COPILOT_HOME`. Accounts come from matching `agents[].configDir` in `~/.legion/configuration/personalisation.json` and use the recorded command when provided. A leading `~/` or `~\` expands against the Legion home. Linked spellings of the same folder run once.
- Plain install skips completed steps. Update refreshes installed copies, including disabled ones, and uses the install steps when a copy is missing. Legion reads versions from manifests on disk and checks marketplace registration and enabled state through the CLI. Missing recorded folders can be created by installation.
- Install, update and uninstall show the plan, ask `Apply? [y/N]` once, then print each step as it runs. Skipped targets appear as `already done`. A plan with no work prints `Nothing to do` and exits successfully without asking. A failed target appears as `failed: <reason>` in the results table. Other targets continue, and the command exits 1 after printing the table and result. `-Yes` skips the question. `-DryRun` reads listings, shows the commands it would run and ends with `Nothing changed (dry run).`
- For install and update, a registered target whose command is missing reports a failed row. Uninstall reports that it skips folders whose command is missing.
- The update one-liners are in the [README](../README.md#update-and-uninstall). Run `legion` in the repository afterward to load the updated skills.


### Running sessions and the stop event

Each `legion mcp` server and `legion run` wrapper registers its process id in its legion home's `.legion/run/` folder. An update stops legion processes registered under its own legion home only. A full uninstall uses the same stop event immediately after approval, before removing plugins, changing PATH or purging the knowledge hub.

On Windows, a plain update with a registered ancestor refuses before changing any plugins or starting the stop event or uv. The ancestor keeps the tool files open. Close your Legion session, run `legion update` from a terminal, then reopen Legion. Update checks and dry runs are available from the session.

Registered processes check for the stop file about once a second. A `legion run` wrapper ends the account's whole process tree by process id. The calling process and its ancestors are exempt. On macOS and Linux, an update asks you to restart the calling session if an ancestor is registered. Legion waits up to 30 seconds for the other registered processes to exit, even after they remove their registration files. On Windows it also waits for the registered processes' Legion launchers to exit so they release the executable. It removes stale registrations. If any process remains, legion lifts the stop file and asks you to close those sessions and run the command again. The uv step is skipped. A full uninstall also skips plugin, PATH and hub changes in this case.

On macOS and Linux, uv replacement and removal run in the foreground and report their result in the terminal. A failed foreground uv install reports the failure in the terminal and exits with status 1. On Windows, an install or update running from the installed uv tool schedules replacement after it exits. The command reports when it schedules the helper. The detached Python helper waits up to 60 seconds for Legion to exit before running uv or purging folders. It records uv's output, exit code and handover errors in `<home>/.legion/temp/uv-tool.log`. `legion status` flags failures recorded in the handover log. A wait timeout skips uv and folder deletion. The helper still removes the stop file and any temporary constraints. If the helper is interrupted, the stop file expires after ten minutes. On Windows, if uv reports locked files, close the sessions that have them open and retry from a separate terminal.

`legion run` repeats the session id Codex reports as `legion: session id <id>` on stderr. The overseer can resume a stopped Codex member when that id is in the handoff. It reruns Claude and Copilot members, and Codex members without a recorded id, from the handoff's task file. See the [task loop](../plugins/legion/skills/legion-overseer/references/task-loop.md#legion-run-and-updates).

### Overseer self-update

The overseer uses `legion update --self --yes` from its repository after the owner approves the update. The command checks for a saved handoff, a live lease owned by the calling session, and a registered launcher. It prints `skip: self-update (already current)` when there is no update to apply and leaves the session running. If another registered launcher or member is running, self-update refuses and gives its process ID. Claude uses the conversation saved by the launcher. Codex uses the thread ID saved with its lease. For Copilot, the owner supplies the current conversation ID from `/session id` for `--session-id <id>`.

The detached worker waits for the request to exit, stops the registered sessions and waits for their launchers, then runs the update from a separate uv environment. It stops on a failed target before replacing the installed command. If uv replacement fails, it restores the previous command. Plugin changes completed by earlier steps remain applied. The worker starts the saved account with its original repository, channels and arguments, adding the CLI's resume arguments for the current conversation. It opens a new console on Windows and uses a detached pseudoterminal on macOS and Linux. The resumed overseer takes the stale lease and continues from the handoff.

The repository's `.legion/temp/self-update.log` holds the result, including any failure to restart. The worker writes diagnostics to `<home>/.legion/temp/self-update.err` and keeps the uv recovery copy under the same directory while replacing the tool. The resumed overseer reports the result on every connected channel. The original shell stays available on macOS and Linux. Check the result file or a connected channel to follow the detached session. If a stop or relaunch fails, the worker also tries to send the result to the configured Telegram destination. A still-running overseer retains the lease. After a failed relaunch, open Legion in the repository and use the saved handoff.

When the installed version is 0.23.1, give the running overseer this command or point it to this guide in the checkout. It starts the same update command from the newer release with uvx. Run it from the launched overseer's repository after a release with self-update is published.

```text
uvx --isolated --refresh-package legion --from "legion[mcp] @ git+https://github.com/BlackGad/ps-legion-marketplace.git@stable#subdirectory=plugins/legion" legion update --self --yes
```

For a checkout, use `uvx --isolated --from "<checkout>/plugins/legion[mcp]" legion update --self --yes --source "<checkout>"`. An installed command that supports self-update opens the separate uv environment itself.

### Installer parameters

For macOS and Linux shell options, put arguments after `sh -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/BlackGad/ps-legion-marketplace/main/install.sh | sh -s -- --update --yes
```

| Shell option | Windows parameter | Legion command or option |
| --- | --- | --- |
| Default | Default | `legion install` |
| `--main` | `-Ref main` | Use the main archive and pass `--ref main` to install or update |
| `--update` | `-Update` | `legion update` |
| `--uninstall` | `-Uninstall` | `legion uninstall` |
| `--purge` | `-Purge` | `legion uninstall --purge` |
| `--source <source>` | `-Source <source>` | Select a marketplace repository or checkout for install or update |
| `--dry-run` | `-DryRun` | `--dry-run` |
| `--yes`, `-y` | `-Yes` | `--yes` |

For options, use the script-block form in Windows PowerShell.

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/BlackGad/ps-legion-marketplace/main/install.ps1))) -Update -DryRun
```

`-Source <source>` in PowerShell and `--source <source>` in the POSIX bootstrap select a GitHub marketplace or a local checkout for install and update. The default is `BlackGad/ps-legion-marketplace`. To test a checkout on Linux, run `sh /work/legion/install.sh --source /work/legion`. Legion applies changes to an explicitly selected checkout even when its version stays the same. A repeat with identical contents and registrations does no work. `-HomeRoot <folder>` uses that folder as the home for `~/.legion` and the files the installer writes, for a test or an alternate home; the default is your real home. `-Ref release|main` picks the track (default `release`, the `stable` branch). To follow `main`, add `-Ref main` to the install line; to switch tracks, select the desired source and ref explicitly. The catalog comes from the bundled `tools.json` for the default source on the release track, and from the remote `tools.json` of the chosen source and branch otherwise (`-Ref main` included); a local checkout as source uses its own file. Remote dry runs read it once. The shared catalog stays readable by the 0.23.1 installer. Account adoption gets its file rewrite list from the individual tool descriptors.

To add a signed-in account folder, run `legion add codex <existing folder>`. The folder must exist; setup copies it into `~/.legion/agents/<id>`, rewrites the folder's own absolute paths in the files the tool's descriptor lists (`adoptRewrite` in `tool.json`; credentials and history are copied as they are), and records the copy. The original folder stays untouched, so the copy has its own login; the account then starts through `legion --account <id>` or `legion pick`. A target folder that already has files is refused, and so is a different folder for an existing id. Setup installs a missing CLI, and legion only where a folder has none, under the same approval; moving an installed legion to a new version is the update flow's job. Only the owner logs in if the CLI asks. An account's existing program can instead be registered through the tool's account guide.

### Version checks and catalog revisions

Overseer sessions check for a newer legion version once a day and show a notice when one is available. Say "check legion updates" for a fresh check, or "update legion" to start the update conversation. A launched overseer can [update itself](#overseer-self-update) after approval. Checks never install updates. Update checks always read the `plugin.json` on the `stable` branch, which moves only when a release is published, so a notice always means a published release. That holds for an install that follows `main` too: the notice compares with the latest release, not with `main`; `legion update` itself follows the marketplace track the install registered. The notice names `legion update`. Run `legion update --check --force` for a fresh check; `--force` refreshes `~/.legion/temp/update.json` and reports check failures.

### Tools catalog

Three files describe the tools legion knows:

- `plugins/legion/tools.json` lists the supported coding CLIs (claude, codex, copilot): how to find each one, its account folder variable and its plugin and hook commands. It is generated from each tool skill's `tool.json`, never edited by hand.
- `plugins/legion/helpers.json` lists the helper programs `legion tool install --tool <id>` can install, such as git or node, with their checks and download steps.
- `tools/generate.py` rewrites `tools.json` and the editor schemas. Run it after changing a `tool.json`.

### Prerequisites and CLI guides

For optional prerequisite hints, use `legion tool install --tool <helper-id>`. Add `--yes` only after choosing to install. See [repository helper sources and approval policy](../plugins/legion/skills/legion-repository/references/tools.md).

Use the dedicated tool guides for CLI installation, account setup, plugin commands, and provider limitations:

- Claude Code: [installation](../plugins/legion/skills/legion-claude/references/install.md), [accounts](../plugins/legion/skills/legion-claude/references/accounts.md), [plugins and hooks](../plugins/legion/skills/legion-claude/references/plugins.md).
- Codex CLI: [installation](../plugins/legion/skills/legion-codex/references/install.md), [accounts](../plugins/legion/skills/legion-codex/references/accounts.md), [plugins and hook trust](../plugins/legion/skills/legion-codex/references/plugins.md).
- Copilot CLI: [installation and plugin commands](../plugins/legion/skills/legion-copilot/references/install.md), [accounts](../plugins/legion/skills/legion-copilot/references/accounts.md).

### Manual CLI installation

Run these in the selected account's environment. Setup normally installs legion before login. The linked tool guides cover account selection and hook approval.

Claude Code:

```text
claude plugin marketplace add BlackGad/ps-legion-marketplace#stable
claude plugin install legion@ps-legion-marketplace
```

Codex CLI:

```text
codex plugin marketplace add BlackGad/ps-legion-marketplace --ref stable
codex plugin add legion@ps-legion-marketplace
```

Copilot CLI:

```text
copilot plugin marketplace add BlackGad/ps-legion-marketplace#stable
copilot plugin install legion@ps-legion-marketplace
```

Codex hooks need one-time trust through `/hooks` in the selected account. Reinstalling preserves trust. Codex updates refresh all Git marketplaces in that account, including unrelated ones.

## Uninstall

Uninstall then install is an upgrade: your settings, notes and account folders stay in place. The one-liners are in the [README](../README.md#update-and-uninstall).

Uninstall previews the removal plan, asks `Apply? [y/N]`, prints progress, and ends with a results table and a result line. Use `--yes` to approve the plan or `--dry-run` to preview it. It stops registered Legion sessions and waits for their launchers on Windows, then runs each target CLI's plugin and marketplace removal commands. If a target fails, the other targets continue and the command exits 1 after the table. On macOS and Linux, uninstall runs `uv tool uninstall legion` and reports the completed removal. On Windows, the helper runs it after Legion exits and plain uninstall names the handover log. Your settings, notes, account folders, logins, repositories, CLIs and uv stay, along with uv's PATH entry.

Setup also puts the knowledge hub under git so a bad compile or a mistake can be undone: a new hub, or an existing hub that is not a repository, gets `git init`, a `.gitignore` with `.sessions/` and one first commit (a repeat prints `skip: knowledge hub git repository (already done)`; a hub that already is a repository is left alone). The commit uses your own git identity, or `legion` / `legion@localhost` for that repository only when none is set; the global config is never changed. Without git, setup prints one line and continues. `legion status` says when the hub is not a repository.

When knowledge is enabled, uninstall also removes the llm-wiki plugin and marketplace from the registered accounts and keeps the hub and its pointer. Legion manages llm-wiki only in accounts. Outlanders keep any manual wiki installs.

If the bootstrap needs to download uv for uninstall, it keeps the verified executable in a separate system temp folder for the detached helper to use. The bootstrap removes its project environment and leaves the executable folder for system temp cleanup. The helper deletes approved purge folders when `uv tool uninstall legion` succeeds or when the tool is already absent. If uv fails to launch or exits with an error, the helper retains those folders and the log. It closes the log before deleting its folder and retries locked files during deletion on Windows. A directory link is removed without deleting its target.

For a full removal, run `legion uninstall --purge` or use `-Purge` in the PowerShell bootstrap or `--purge` in the POSIX bootstrap. Purge also removes recorded portable-tool entries from the user PATH and deletes `~/.legion`, including settings, notes, account folders, Legion-made logins and the footprint index, after uv succeeds. On Windows the deletion is scheduled. Purge does not link to a log it deletes. If a hub exists at `~/.legion/knowledge`, purge asks separately whether to delete it. `--yes` and `-Yes` approve only the plan. Declining or closing input at the hub question keeps the hub. Outlander folders and repositories stay. Plugin caches under the standard CLI folders belong to those CLIs. To repeat removal after the `legion` command is gone, use the bootstrap with its uninstall or purge option.

## First run

Owner files are `~/.legion/configuration/personalisation.json` and `configuration/notes.md`; the update cache is `temp/update.json`, tool downloads use a throwaway `temp/install-*` folder, `run/` holds the running sessions' registrations and the stop file, and every account starts through `legion run`, which belongs to the uv tool.

The footprint index is `~/.legion/footprints.json`. It records what legion needs to find again: the user PATH entries of the portable tools it installs.

After installing legion, ask any tool to add accounts, or run setup from a clone or installed plugin folder:

```text
legion add claude
```

This shows the plan and asks `Apply? [y/N]`. Answer `y` in the terminal or pipe a yes into the command. Pressing Enter or closing input declines. Use `--yes` when you have already approved the plan.

Setup installs a missing CLI first, then adds the account and installs legion before login. It shows plugin commands and their output as they run. For a new account, the result includes `log in: <command>`, which starts the tool itself with the account's folder variable set. Run it once outside legion and log in. `legion` and `legion pick` start only the overseer. If CLI installation fails or needs a new terminal, retry the add after the tool is available. If the account was added and the plugin failed, run `legion setup` to finish. Setup writes:

- `~/.legion/configuration/personalisation.json`: account folders and any later verified channel bindings. Validated against `plugins/legion/schemas/personalisation.schema.json`. It never holds tokens.
- `~/.legion/configuration/notes.md`: free-form preferences and lessons.

For every Claude account, setup also adds the permission rule `Bash(LEGION_ROLE=member *)` to its `settings.json`, so the overseer can start legion members without a prompt; other settings and rules stay. It also installs the `legion` command as a uv tool (`uv tool install`, from the plugin copy, pinned to the plugin's `uv.lock`). Inside the same yes, setup and the installer run `uv tool update-shell`, which edits your shell profile or, on Windows, your user PATH so the tool folder is on PATH; the plan shows that line before consent and a dry run lists it. If the tool is already installed and its folder is missing from PATH, `legion status` reports it and setup offers to run just `uv tool update-shell` for the command. On Windows this checks the saved user and machine PATH. On macOS and Linux it checks the shell's current PATH. Without a yes nothing is installed or edited. Legion records only what it adds itself: a permission rule you already had stays yours. Open a new terminal if needed.

Run `legion status` before making changes. It has separate `accounts` and `outlanders` headings and reads plugin versions from disk without launching the coding CLIs. The outlanders heading appears when a stock command is available. `legion status --json` and the MCP `status` tool include an `outlanders` list. Before setup, with no stock CLI on PATH and outside an orchestrated repository, status prints:

```text
no legion settings yet. Run legion add <tool> to add an account.
knowledge: off (not asked)
repo none
```

`legion setup` finishes setup for recorded accounts. A completed plan prints `Nothing to do.` The plan shows prerequisite repairs, such as a Node.js that is on PATH but does not run, on its `cli` line, so apply does nothing the plan did not show. With `legion --home <home> setup`, the commands setup writes carry that home, so `legion` reads that home and not the real one. Adding the same tool again without an explicit id creates another account. When the plan needs the remote installer, it names the download.

### Legion commands

These are the main commands; `legion help` lists all of them:

| Command | What it does |
| --- | --- |
| `legion setup [--yes]` | Finish setup: settings, commands, legion in every account |
| `legion setup --knowledge/--no-knowledge` | Turn llm-wiki on or decline it. A hub that needs no changes and accounts with the pinned plugin already enabled appear as `already done` in the setup results. Turning knowledge off after an opt-in removes the wiki plugin and marketplace from every account and keeps the hub and its pointer. If knowledge was never enabled, a decline leaves manual installs in place. The choice is part of the setup plan. Use `--yes` to apply it without a prompt. |
| `legion add <tool> [<folder>] [--yes]` | Add a claude, codex or copilot account, in a new folder or a copy of an existing one |
| `legion remove --id <id>` | Remove an account; keep its folder |
| `legion status` | Show accounts, outlanders, channels, knowledge and this repository |
| `legion [--no-channel]` | Start the overseer here, set up if needed; resumes the last conversation (see [launcher defaults](../plugins/legion/skills/legion-personalisation/references/launcher.md)) |
| `legion pick [--no-channel]` | Choose which account leads the overseer |
| `legion limits [--id <id>]` | Show each account's usage windows (how much is left in %, reset time, status; JSON has `used` and `left`), or one account's; `unknown` when the tool gives no readout |
| `legion lease take\|status\|release` | Take, check or release this repository's overseer lease; the overseer skill runs it |
| `legion install [--source <source>] [--ref release\|main] [--dry-run] [--yes]` | Install legion in the available CLI targets and set up the legion command with uv |
| `legion update [--source <source>] [--ref release\|main] [--dry-run] [--yes]` | Update every listed target along the registered marketplace track (release or `main`). One question covers them all, and `--yes` skips it. On Windows, run from a terminal after closing the Legion session. Registered processes exit before uv replaces the command |
| `legion update --check [--force]` | Check for a newer release; `--force` refreshes the cached result |
| `legion update --self --yes [--source <source>] [--ref release\|main] [--session-id <id>]` | Stop, update and resume the launched overseer. Supply the conversation ID for Copilot |
| `legion uninstall [--purge] [--dry-run] [--yes]` | Remove legion (running sessions are stopped first); purge also removes settings, notes and legion-made logins |
| `legion help` | Show this list |

The owner command also accepts these account, channel and repository verbs:

| Command | What it does |
| --- | --- |
| `legion account add --id <id> --tool <tool> [--provider foundry --endpoint <url> --deployment <name> --default <name>]` | Add or repair a subscription or Foundry account; with `--config-dir <dir>` it copies an existing folder into the new account; with `--command <path>` it sets or changes the account's CLI program |
| `legion account rename --id <id> --new-id <new>` | Rename an account id; keep its folder |
| `legion account models --id <id> [--deployment <name> --default <name>] [--alias <a>=<b>] [--standard] [--list]` | Show or change a Foundry account's deployments |
| `legion channel add --id <id> --kind telegram --target <chat> --account <account>` | Bind a Telegram destination to an account |
| `legion channel check --kind telegram --account <account>` | Show channel prerequisites without launching a tool |
| `legion channel remove --id <id>` | Remove a channel binding |
| `legion repository setup --repo <path> [--mode shared] [--gate <command>] [--rules-file <file>]` | Set up repository orchestration |
| `legion dismiss [--yes]` | Release this repository from the legion |

On the first owner command after the installed version changes, Legion checks for pending setup work. It applies steps that need no new approval, such as restoring missing notes, and lists any steps you can approve by running `legion setup`. It records the version in `~/.legion/version` after a successful check. If the check fails, Legion prints the error to stderr, continues your command and retries next time. Machine commands such as `mcp`, `run`, `lease`, `inbox`, `telegram`, `validate`, and `update --check` skip this check, as do JSON output, dry runs and uninstall.

`dismiss` shows its plan, then asks `Apply? [y/N]` (closed input means no; `--yes` skips). It removes what setup added outside `.legion/` (Private: the `.legion/` line in `.git/info/exclude`; Shared: the `.gitignore` lines and the legion section of the rules file, left uncommitted for review), removes lane worktrees under `.legion/temp/`, runs `git worktree prune`, then deletes `.legion/`. A Private repository warns first that its legion history goes with the folder. It refuses in a member session and while another live session holds the overseer lease. A repository without legion prints `skip: legion in this repository (already dismissed)`.

A bare `legion` creates Private orchestration by default; ask the tool for [Private or Shared](../plugins/legion/skills/legion-repository/SKILL.md#choose-where-notes-live) storage and a repository check command when needed. Each repository's overseer has its own Telegram bot folder, `~/.legion/channels/telegram/<channel id>`; the launcher points the session at it.

The console and each connected channel are equal; the rules are in the [personalisation skill](../plugins/legion/skills/legion-personalisation/SKILL.md#identity-and-hard-rules).

Runs pass no model flag by default; a flag is passed only when owner notes name a model or allow a tier ([model tiers](../plugins/legion/skills/legion-overseer/references/task-loop.md#model-tiers)). Without owner preferences the overseer follows [default routing](../plugins/legion/skills/legion-overseer/references/task-loop.md#default-routing).

The [launcher guide](../plugins/legion/skills/legion-personalisation/references/launcher.md) covers saved launches, account selection and channel defaults.

Foundry agents use one Azure endpoint per agent, such as `https://<resource>.cognitiveservices.azure.com`, and the owner's `az` login. Setup can copy an existing folder without changing its provider settings. For an explicit provider configuration request, use the matching account guide with values already supplied by the owner. Model preferences for work remain in notes.

Manage accounts through the account's tool skill. See the shared [terms](../plugins/legion/skills/legion-overseer/references/terms.md) for account actions and model preferences.

## Modes

The `legion` plugin lets Claude Code, Codex CLI and GitHub Copilot CLI share one way of running multi-agent work in a repository. One session acts as the **overseer**: it plans, hands tasks to working agents on your other accounts, reviews their results and keeps a tracked handoff, so any of the three tools can pick the work up later. Working agents (the **legion**) do one task each and report back.

A hook at session start decides the mode:

| Mode | When | What happens |
| --- | --- | --- |
| Member | The overseer started this process with `LEGION_ROLE=member` and `LEGION_TASK` | One task, local commits, a report; no other agents |
| Overseer | You say continue, progress or next in a repo with `.legion/state/` | Takes the lease, reads the handoff, continues the work |
| Setup | You ask to set up orchestration | Creates the repo layout |
| Ad hoc | Anything else | Nothing changes; an orchestrated repo shows one hint line |

A session you start yourself is never a legion member.

The hook is a small Python script in the plugin copy. `uv` starts it with Python 3.12 or later, offline and without downloads. When uv or Python is missing, the hook prints one line asking you to run the legion installer, and the session starts normally. Update notices use the local cache.

Legion passes the configured home when it starts an account for the overseer or a member run. The hook uses the login home by default. When starting a host directly for an alternate `legion --home` installation, set `LEGION_HOME` to the same absolute path.

The hook reports whether a repository lease file is present. `legion lease status`, `legion lease take`, and `legion status` check whether the process in the lease is still live.

On Windows, `legion status` also warns when a bound channel's launch flag is absent from a recognized native CLI session, including outside an orchestrated repo. Members, unknown or ambiguous accounts, and unsupported or unreadable sessions stay silent.

## Security: legion runs without a sandbox

Legion workers run with your full user rights so they can edit, run checks, and commit using your installed SDKs, user PATH, and tools. Sandboxes can hide installed tools; agents then download their own SDKs and drift from your machine's setup. Before the first legion dispatch, the overseer explains this and asks once. It records approval or refusal in notes and follows that answer without asking again.

The overseer starts legion only inside an assigned repository worktree and only for planned tasks. Workers must stay inside that repository or worktree, never push or touch other folders, and never download or install an SDK, runtime, package manager, or tool. If a needed tool is missing or the wrong version, they stop and report it so the overseer can ask you. These safeguards are instructions, not an enforced access boundary. Reviewer runs keep their read-only restrictions.

To opt out, leave legion unused. To limit its use, route legion only to accounts you trust.

## Files and folders

| What | Where | Git history |
| --- | --- | --- |
| Personalisation and notes | `~/.legion/configuration/` | no |
| The `legion` command (a uv tool; its folder is `uv tool dir`, the command in `uv tool dir --bin`) | uv's folders | no |
| Running legion sessions and the stop file | `~/.legion/run/` | no |
| New account config folders (a copied folder lands here too) | `~/.legion/agents/<id>/` | no |
| Portable prerequisite tools | `~/.legion/tools/<tool>/` | no |
| Cross-project knowledge wiki (optional, llm-wiki) | `~/.legion/knowledge/` | no |
| Repo workflow configuration | `<repo>/.legion/configuration/` (`project.json`, `workflow.md`) | inner for Private; code repo for Shared |
| Repo scripts and their index | `<repo>/.legion/scripts/`, `index.md` | inner for Private; code repo for Shared |
| Overseer state | `<repo>/.legion/state/handoff.md`, `history/` | inner for Private; code repo for Shared |
| Local specs and plans | `<repo>/.legion/docs/specs/`, `plans/` | no |
| Agent scratch files | `<repo>/.legion/temp/` | no |

The [repository layout](../plugins/legion/skills/legion-repository/references/layout.md) defines ignores and Git ownership. Private is identified by a `.legion/.git` folder. Its outer ignore is local to Git and shared across worktrees. Shared uses the code repo's `.gitignore` and rules file. For explicit choices use `legion repository setup` (see the commands above). Handoff, history, and roadmap commits use the Git repository that owns `.legion/state`.

## Skills

| Skill | Job |
| --- | --- |
| `legion-personalisation` | Your accounts, routing, channels and projects |
| `legion-repository` | Repo layout, scripts index, rules-file block |
| `legion-overseer` | Takeover, milestone flow, task loop, reviews, handoff |
| `legion-member` | The working agent's contract and report |
| `legion-knowledge` | Consult and grow the cross-project wiki |
| `legion-update` | Check versions and update legion after approval |
| `legion-claude`, `legion-codex`, `legion-copilot` | Run each CLI, check its limits, add or rename accounts |

## Works with method skills

legion decides who does the work, where, and where the record lives. How one piece of work is done (brainstorming, planning, test-driven development, debugging, code review) comes from the method skills you have installed, such as superpowers. The repo rules block that `legion-repository` adds sends tasks to your legion accounts and plans to the repo's configured paths.

## Releases

If you push a commit to the release pull request by hand, run the `release` workflow once through `workflow_dispatch` to post the `gate` and `integration` statuses on the new head.

## Development

Repository rules, tests and the release flow are in [AGENTS.md](../AGENTS.md).

License: [MIT](../LICENSE).
