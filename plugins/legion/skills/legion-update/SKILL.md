---
name: legion-update
description: Use when the owner asks about legion updates or wants to update legion, or the session notice says a newer legion is available; never in member mode.
---

# Update legion

Never use this skill in member mode. Plain updates work without repository setup or an overseer lease. A session notice offers an update. The owner authorizes the update.

Speak to the owner in plain words. Show a command only when they ask for it or must run it themselves.

1. Run a fresh check, even after a session notice, using [commands.md](references/commands.md).
2. Tell the owner the installed and available versions. If no newer version is available, say so and stop. If the check fails, explain that the versions could not be confirmed; use the fallback in the reference without claiming a version you could not read.
3. Before asking to restart your own launched overseer session, let running members finish or record them and their next steps in the handoff. Wait for their sessions to close before running self-update. Use the owner's yes if they already gave it, or ask whether they want to update. If they decline, stop. For a self-update, save the handoff, keep the lease, and tell the owner on every channel that you are about to restart. Use the self-update instructions in the reference, including the first release that offers it. Let Legion stop and restart you.
4. When you come back, take the stale lease, read the handoff and update result, and report on the console and every connected channel. Continue the saved work after reporting. For a plain update, follow the terminal and result instructions in the reference. After it succeeds, ask the owner to run `legion` in the repository to reopen the conversation.
