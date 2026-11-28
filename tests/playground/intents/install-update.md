---
covers: plugins/legion/src/legion/services/lifecycle/*, plugins/legion/src/legion/services/installation/*, plugins/legion/src/legion/services/accounts/*, plugins/legion/src/legion/cli/*, plugins/legion/tools.json, plugins/legion/helpers.json
os: both
budget: 25
---
Goal: install legion from this checkout, add an account, then update it.

You play: a new owner on a fresh machine. The three coding CLIs are installed, nobody is logged in, and you have no tokens. Work only through the container; do not log in anywhere.

Do, in this order, with your own judgement about what to try in between:
1. Install legion from the checkout (`sh /work/legion/install.sh --source /work/legion` on Linux or `install.ps1 -Source <checkout folder>` on Windows, answer the question like an owner would). Then run `legion status`.
2. Add an account (`legion add claude` or another tool). Look at what it says about logging in.
3. Update (`legion update`, then `legion update --source <checkout folder>`). Run the same install and update again.

Watch for:
- Every question is visible before it waits for input, and says what a yes does.
- Progress shows during every wait longer than a few seconds; silence of more than about 10 seconds is a finding.
- One plain result line at the end of each command, in words an owner understands.
- Exit codes: 0 for done or declined, non-zero only for a real failure.
- A repeat run says `skip: ... (already done)` and changes nothing.
- Files left behind: list `~/.legion` and the account folders after each step.
- Time every step.
