# Status and account limits

Use the `status` and `limits` tools on the plugin's `legion` MCP server first. If discovery or startup fails, use `legion status --json` or `legion limits --id <id> --timeout 13 --json`. Report that fallback. Members never call these tools.

Status shows `accounts` and `outlanders` separately. Outlanders are the default folders of stock CLIs on PATH. Use IDs from `accounts` when choosing who runs a task or checking limits.

For limits, choose account IDs from configuration, the owner's request and prose preferences. Scripts never parse notes. Ask only when scope or paid-use permission is unclear. Always pass an explicit, nonempty `accounts` list. One call may name several accounts. The table shows how much of each window is left, and JSON has `used` and `left`. Unknown results stay unknown.

Every limits call pings each named account with a real minimal prompt through its own program, consumes quota and is billable on paid accounts. It creates and removes a disposable ping folder; Codex leaves a session record in the account's sessions folder. Foundry runs `az account get-access-token` instead and reports only the token check. This does not prove deployment access.

The CLI timeout defaults to 120 seconds per account. MCP checks the selected accounts in parallel within a 25-second per-call budget. Each probe uses the requested timeout capped at 13 seconds, leaving time for CLI startup, probe cleanup, and returning results. Cancelling a call or disconnecting stops its probes and removes ping folders. A new connection starts a new server process.
