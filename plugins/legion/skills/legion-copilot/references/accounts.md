# Copilot CLI accounts

Add and copy a folder through `legion add`, as [create.md](../../legion-personalisation/references/create.md) describes, and follow the [shared account rules](../../legion-personalisation/references/fields.md#account-setup). For a new folder the owner logs in once by starting `copilot` with `COPILOT_HOME` set to the account's folder; setup's `log in:` column shows the command. Never handle tokens or credential files.

## Foundry agent

Use this flow instead of the subscription login flow above. One agent uses one endpoint. Copilot Foundry agents support **Claude deployments only**. GPT deployments are out of scope. Use `legion --home <home>` for an alternate home.

Use these commands for a Foundry provider configuration request; ordinary setup can copy an existing folder and keep its provider settings. Ask the model question and discover deployments as [Foundry model mapping](../../legion-personalisation/references/foundry-models.md) says. These examples show explicit choices:

```powershell
legion account add --id <id> --tool copilot --provider foundry --endpoint https://<resource>.cognitiveservices.azure.com --deployment claude-opus-x --deployment claude-sonnet-x --default claude-opus-x
```

Add writes the provider entry. It writes no persistent Copilot provider config. The provider default must name a configured deployment. The runner leaves run-time model selection to the account's CLI default unless owner notes name a model or allow a tier; follow [Foundry routing](../../legion-overseer/references/task-loop.md#foundry-runs). Repeating the same id, tool, provider, and endpoint prints `skip: account <id> (already done)`.

For a requested provider configuration change, reuse the supplied settings and apply the complete replacement with:

```powershell
legion account models --id <id> --deployment claude-opus-x --deployment claude-sonnet-x --default claude-opus-x
```

An identical result prints `skip: models <id> (no changes)` and writes nothing. Then finish with `legion setup`, as the model mapping guide describes.

Start it with `legion --account <id>` or `legion pick`. Legion sets `COPILOT_HOME` and points `COPILOT_PROVIDERS_CONFIG` to a per-launch file under `~/.legion/temp/`. This is the approved exception to the no-token-storage rule: Copilot's provider file holds a **fixed bearer token**. The runner creates it with the user profile's Windows permissions, or mode `0600` on other platforms, before writing the token. It deletes the file in `finally` when Copilot exits, including an error exit. Never read or log it.

Each Copilot Foundry launch also removes expired legion provider files left by interrupted runs, using the token expiry recorded in their filenames.

The runner prints `token valid until HH:mm; after that restart with: legion run --account <id> -- --resume` in local time. The token does not refresh during that run. After expiry, exit and restart with `legion run --account <id> -- --resume` in the same folder to obtain a fresh token. A missing CLI stops with `copilot is not installed; run legion tool install --tool copilot`.

Account selection and launch-field examples are in the [launcher guide](../../legion-personalisation/references/launcher.md). The base `copilot` executable may be the `command` for a separately named account such as `agent-a`; account ids must not shadow commands on PATH.
