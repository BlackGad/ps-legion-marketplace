# Codex CLI accounts

Add and copy a folder through `legion add`, as [create.md](../../legion-personalisation/references/create.md) describes, and follow the [shared account rules](../../legion-personalisation/references/fields.md#account-setup). For a new folder the owner logs in once by starting `codex` with `CODEX_HOME` set to the account's folder; setup's `log in:` column shows the command. Never handle tokens or credential files.

## Foundry agent

Codex is **not yet proven against Foundry**. Use this flow when GPT access exists and the owner wants to verify it. It replaces the subscription login flow above. One agent uses one endpoint. Use `legion --home <home>` for an alternate home.

Use these commands for a Foundry provider configuration request; ordinary setup can copy an existing folder and keep its provider settings. Ask the model question and discover deployments as [Foundry model mapping](../../legion-personalisation/references/foundry-models.md) says. These examples show explicit choices:

```powershell
legion account add --id <id> --tool codex --provider foundry --endpoint https://<resource>.cognitiveservices.azure.com --deployment gpt-x --default gpt-x
```

Add writes the provider entry and the account's `config.toml`. It sets `model`, `model_provider = "legion-foundry"`, `/openai/v1` as the endpoint path, and `wire_api = "responses"`. The auth command uses the full `az` path and the cognitive-services token resource, with `refresh_interval_ms = 1800000`. Other config keys remain. Repeating the same id, tool, provider, and endpoint prints `skip: account <id> (already done)`.

For a requested provider configuration change, reuse the supplied settings and apply the complete replacement with:

```powershell
legion account models --id <id> --deployment gpt-x --default gpt-x
```

The default must be selected. An identical result prints `skip: models <id> (no changes)` and writes nothing. Then finish with `legion setup`, as the model mapping guide describes.

Start it with `legion --account <id>` or `legion pick`. Legion sets `CODEX_HOME`; the provider config supplies the endpoint and token command. Resume only with the same agent and folder. A missing CLI stops with `codex is not installed; run legion tool install --tool codex`. Verify an actual GPT response before reporting this path as working.

Account selection and launch-field examples are in the [launcher guide](../../legion-personalisation/references/launcher.md). The base `codex` executable may be the `command` for a separately named account such as `agent-a`; account ids must not shadow commands on PATH.
