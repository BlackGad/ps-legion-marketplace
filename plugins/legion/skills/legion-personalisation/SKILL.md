---
name: legion-personalisation
description: Use when any legion skill starts, setup is missing or invalid, or the owner asks to add/update/remove an agent or account, change preferences or channels, add a minion, extend the legion, or retire a minion.
---

# Owner personalisation

## Identity and hard rules

For any agent tool operation, read the selected account's tool skill using [fields.md](references/fields.md). Stop if that tool skill is missing; do not guess commands or capabilities.

Use the shared [terms](../legion-overseer/references/terms.md) to recognize the owner's words for accounts and model preferences.

This context provider grounds every legion mode. Read both the owner's settings and notes before applying another legion skill. Use [fields.md](references/fields.md) for their locations and validation, and [create.md](references/create.md) for an alternate home. Speak to the owner in plain words; show commands only when asked or when they must run them themselves.

- Never read, print, log, or store credentials or token files. Store account identity, folders and verified channel bindings only.
- Ask the owner exactly one question per message or dialog, then wait for the answer before asking the next. Never bundle questions or offer "accept all recommendations" for a list of choices. Put the recommended option first.
- The session console and each connected channel (for example Telegram) are independent channels with the same priority. Ask every owner question in all of them: in the console as a normal message, and through each connected channel with the same options in plain words (recommended first). An answer from any of them counts; then confirm in the others that it is answered. While a channel is connected, never use a blocking terminal question dialog (for example Claude Code's AskUserQuestion, or any prompt that waits for terminal input): it blocks the session and incoming channel messages go unanswered. Keep the session responsive while waiting. When a request arrives through a connected remote channel, first acknowledge it there at once, then work and send the real answer as a follow-up. If the channel supports reactions (for example Telegram), confirm receipt with an emoji reaction on the owner's message instead of a text message, and vary the emoji; send a short text acknowledgement instead only when text better describes the next operation (a long or multi-step job, a wait, the result of a choice). Without reaction support, send the short message. The owner's notes set the tone of the acknowledgement, reaction included (for example a playful "Your wish is my command."); without a stated tone keep it short and plain. Send progress reports and "done" notices to the console and to each connected channel, in the owner's stated tone and format. Without a connected channel, ask in the session as usual; a dialog is fine then. A legion member never messages the owner; it reports questions to the overseer, who asks on all channels.
- Change personalisation only when the owner requested it. Keep project facts in the repo and personal habits in notes. Save preferences, rulings and lessons that must persist in shared legion files first so work can continue in any tool: personal, cross-repository preferences and lessons in the owner's `notes.md`; repository preferences (routing, models, workflow choices) in `.legion/configuration/workflow.md` under "Repository choices"; current work state, rulings and next action in `.legion/state/handoff.md` (overseer only) or the milestone spec/plan. A tool's private memory may hold only a duplicate: whenever writing there, also save the same fact to the matching shared file.
- Leave legion access consent to the overseer's [first dispatch](../legion-overseer/references/task-loop.md#first-legion-dispatch). Setup asks only which agents to add and for one yes to install legion into them.
- Use unique account names that do not conflict with installed commands. Add new accounts, copy existing account folders, and register an account's existing program through the matching tool skill; see [fields.md](references/fields.md) for naming rules. When the owner gives an existing folder, copy it into the new account, which keeps its settings and login; the original stays as it is. Do not ask them to log in again.
- Every account legion knows needs legion installed. Explain that account setup includes installing legion into that account and get the owner's yes before installation; `legion setup` does it, as [create.md](references/create.md) says. Use approval already given in the conversation. One setup run covers all known accounts.
- Never install a tool, launch an agent, or open an overseer window without authorization. The session launcher offer asks first; direct launcher invocation is itself a request to launch.

## Use the owner's settings

1. Read and validate existing settings, then read notes. If settings are missing, use [create.md](references/create.md); if invalid, explain the validation issues and make only owner-requested corrections. During setup, create missing notes from [notes-template.md](references/notes-template.md).
2. Read repository workflow for repository choices. Keep repository facts there.
3. Follow account preferences in notes. Pass a model only as [model tiers](../legion-overseer/references/task-loop.md#model-tiers) allow. Save new owner preferences using the shared memory rule above. Follow exclusions and careful-use choices. Ask before paid use. Use the selected tool skill.
4. Set up channels only when the owner asks. Route them through their kind skill. Learn knowledge and other preferences from the owner's words after setup. Use the [launcher guide](references/launcher.md) for commands; follow its launch flow only when a launch is requested. Pass explicit choices derived from the owner's words.
5. Re-read changed settings and notes. Check that no secret or client detail entered them.

## Stop and verification

Stop after two failed validation fixes and report the exact issues. If the owner declines to create missing personalisation, continue without legion orchestration. Do not infer a missing account purpose, channel target, or paid-account approval.

- [ ] Settings and notes were both read, or missing files were handled through the create flow.
- [ ] Every settings edit was re-read.
- [ ] New or existing accounts were handled through the selected tool skill, legion was installed with the owner's yes, and no secret was handled.
