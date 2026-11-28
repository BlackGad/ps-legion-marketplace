# Settings fields

Save preferences using the [shared memory rule](../SKILL.md#identity-and-hard-rules). Personal, cross-repository preferences belong in `~/.legion/configuration/notes.md`. Read notes before selecting accounts, dispatching, communicating or knowledge work. Follow stated models, exclusions, account purposes, reviewer and worker order, careful-use choices and paid-use approval. Pass a model only as [model tiers](../../legion-overseer/references/task-loop.md#model-tiers) allow. Save new owner preferences as plain sentences when stated. Re-read after edits. Current owner instructions win. Scripts never parse notes.

Route channel operations through `skills/legion-<kind>/SKILL.md`; stop if missing. Any chosen session may oversee without channels.

| Field | Operational consumer and purpose |
| --- | --- |
| `$schema` | The Python settings writer normalizes the stable editor URL; runtime uses the bundled schema. |
| `schemaVersion` | `legion validate` checks the bundled runtime format. |
| `agents[].id` | Account operations, `legion --account <id>` and member runs select the account by id. |
| `agents[].tool` | Tool discovery chooses account, Foundry and launch adapters. |
| `agents[].configDir` | The account launch, Foundry adapters and root installer select the account folder. Use a concrete folder, never `default`. |
| `agents[].command` | Optional. The account's own CLI program, set only when it is not the tool's stock command; `legion account add --command` stores it. Subscription accounts only. |
| `agents[].provider.kind` | Foundry runner selects provider dispatch; only Foundry is implemented. |
| `provider.endpoint` | Foundry adapters connect to the selected HTTPS host. |
| `provider.default` | Foundry adapters select the default deployment. |
| `provider.deployments[].name` | Foundry picker and adapters use wire deployment IDs. |
| `provider.deployments[].model` | The model-mapping adapter maps model IDs. |
| `provider.deployments[].alias` | The alias adapter maps supported aliases. |
| `channels[].id/kind/target/accounts` | The Python launcher selects the descriptor, destination and eligible accounts and validates bindings. |

Never save secrets. The validator enforces the required fields, unique lowercase ids, channel bindings, the default deployment and the HTTPS host; fix what it reports. Validate every edit with the bundled schema, checking stdout, stderr and the exit code:

```text
legion validate --kind personalisation --path <home>/.legion/configuration/personalisation.json
```

Use the stable editor URL `https://raw.githubusercontent.com/BlackGad/ps-legion-marketplace/main/plugins/legion/schemas/personalisation.schema.json` or omit `$schema`. Unknown fields require setup. Do not invent defaults or copy sample preferences. Use [create.md](create.md) for setup and [notes-template.md](notes-template.md) for headings. Record stated models, exclusions, purposes, careful/paid-use choices, notifications, format, knowledge location and durable lessons. Put unfinished requested work and “Finish my legion setup” under Finish later. Add no consent or status properties. The agent repairs prose references after account/channel changes and reports unresolved wording.

For tool operations read `skills/legion-<agent.tool>/SKILL.md`; stop if missing.

## Account setup

Copy an existing folder into the account and keep its settings and login. Never read credentials or test sign-in. Only the owner logs in, by starting the tool itself with the account's folder variable set, as setup's `log in:` column shows; `legion` and `legion pick` start only the overseer. Use the matching tool's account guide for commands.

Every new, copied, registered or Foundry account needs legion installed in its recorded config folder; setup installs it only where the folder has none, and moving versions is the update flow's job. Explain the install and get the owner's yes; reuse approval already given. Expand `~/` against the chosen home. Only `legion setup` and `legion add` install: a missing CLI through `legion tool install`, and legion, only with `--yes` after the owner's yes. Report setup as incomplete if installation fails or is declined.
