# Claude Code accounts

Add and copy a folder through `legion add`, as [create.md](../../legion-personalisation/references/create.md) describes, and follow the [shared account rules](../../legion-personalisation/references/fields.md#account-setup). For a new folder the owner logs in once by starting `claude` with `CLAUDE_CONFIG_DIR` set to the account's folder; setup's `log in:` column shows the command. Setup also adds the allow rules from `allowRules` in `tool.json` to each account's `settings.json`, so the overseer can start members without a prompt. Never handle tokens or credential files.

## Foundry agent

Use this flow for a Foundry endpoint instead of the subscription login flow above. One agent uses one endpoint. Use `legion --home <home>` for an alternate home.

Use these commands for a Foundry provider configuration request; ordinary setup can copy an existing folder and keep its provider settings. Ask the model question and discover deployments as [Foundry model mapping](../../legion-personalisation/references/foundry-models.md) says. These examples show explicit choices:

```powershell
legion account add --id <id> --tool claude --provider foundry --endpoint https://<resource>.cognitiveservices.azure.com --deployment claude-opus-x --deployment claude-sonnet-x --default claude-opus-x --alias claude-opus-x=opus --alias claude-sonnet-x=sonnet
```

Aliases are optional: `opus`, `sonnet`, `haiku`, or `fable`, each used once. The default must be a selected deployment name. Add writes the provider entry, and `modelPicker` in the account's `settings.json`. Other settings remain. Invalid JSON stops with `settings.json is not valid JSON; fix or remove it`. Repeating the same id, tool, provider, and endpoint prints `skip: account <id> (already done)`.

For a requested provider configuration change, reuse the supplied settings and apply the complete replacement with:

```powershell
legion account models --id <id> --deployment claude-opus-x --deployment claude-sonnet-x --default claude-opus-x --alias claude-opus-x=opus --alias claude-sonnet-x=sonnet
```

`models` replaces the full deployment list. Omit `--alias` to keep aliases for retained names; pass `--alias ''` to clear them. An unchanged result prints `skip: models <id> (no changes)` and writes nothing. Then finish with `legion setup`, as the model mapping guide describes.

Start it with `legion --account <id>` or `legion pick`. Legion sets `CLAUDE_CONFIG_DIR`, the Foundry endpoint, default, and alias variables. Claude uses its own Azure token refresh. Pass a model flag only when owner notes name a model or allow a tier for the run; follow [Foundry routing](../../legion-overseer/references/task-loop.md#foundry-runs). Keep the same agent and endpoint when resuming. A missing CLI stops with `claude is not installed; run legion tool install --tool claude`.

Account selection and launch-field examples are in the [launcher guide](../../legion-personalisation/references/launcher.md). The base `claude` executable may be the `command` for a separately named account such as `agent-a`; account ids must not shadow commands on PATH.
