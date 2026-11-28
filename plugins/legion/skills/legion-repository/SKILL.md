---
name: legion-repository
description: Use when the owner asks to set up or dismiss orchestration in a repository, the overseer finds no .legion/configuration, or a fixed repository step lacks a listed script.
---

# Repository orchestration setup

## Identity and hard rules

For any agent tool operation, read the selected account's tool skill using [setup.md](references/setup.md). Stop if that tool skill is missing; do not guess commands or capabilities.

This planner creates and updates repository facts. Load `legion-personalisation` and its notes first. Preserve all unrelated orchestration files, including installed skills and plugins. Speak to the owner in plain words; show commands only when asked or when they must run them themselves.

- Never overwrite an existing orchestration file without showing its diff. Read before editing and make the smallest change.
- Turn each fixed gate, build, benchmark, or cleanup into an indexed script named in repository workflow; call it instead of rebuilding its commands. See [setup.md](references/setup.md).
- Scripts check current state first; a repeat reports completed work as skipped and changes nothing.
- Repository choices live in workflow, personal preferences in notes, account and channel data in owner settings.
- To take legion out of a repository, run the dismiss command in [setup.md](references/setup.md) after saying its plan; Private loses its history unless `.legion/` was pushed.
- Private keeps the [rules block](references/rules-file-lines.md) in workflow and never edits tracked repo files; Shared adds it to the repo's rules file.

## Choose where notes live

Offer these once for a fresh setup. Put Private first and recommend it:

- **Private (default):** Orchestration notes, the handoff and history stay on this PC in `.legion/`, with their own Git history. Nothing appears in the repo or on GitHub. To keep them on other machines, you can later push `.legion/` to a private repo.
- **Shared:** Orchestration notes are committed with the code. Teammates and other machines see them. Use this for private or team repos.

For an existing setup, retain its mode only when the Git checks in [layout.md](references/layout.md) confirm it. If neither check matches, setup is not finished: ask the owner the setup choice again, with Private as the default, and complete it. Do not add a settings field.

## Create or update

1. Check the layout and gate with the owner: the mode above, which rules file is authoritative if unclear, and the one command that gates a task, if any. Offer missing Git tools through [tools.md](references/tools.md).
2. Run the setup script once as [setup.md](references/setup.md) says; it creates missing files and commits the setup.
3. Re-read the result. Confirm every configured step resolves to an indexed script and `project.json` validated.
