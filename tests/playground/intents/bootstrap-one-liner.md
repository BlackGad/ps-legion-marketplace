---
covers: install.ps1, install.sh, plugins/legion/src/legion/services/lifecycle/*, plugins/legion/src/legion/services/installation/*, tests/repository/*
os: both
budget: 25
---
Goal: use the bootstrap script from the checkout the way the README one-liner does, to install and then update legion.

You play: an owner who pastes the install line into a terminal. On Linux run `sh /work/legion/install.sh --source /work/legion`; on Windows run `install.ps1 -Source <checkout folder>` from the checkout in Windows PowerShell 5.1, and again in PowerShell 7 (`C:\pwsh\pwsh.exe`) after a fresh container if you want to compare. uv is already installed in this container; if you want the case without uv, take it off PATH for one run and say so in the report. Use the checkout as the source, not the network release.

Do:
1. Run the bootstrap with `--dry-run` (`-DryRun`) and read the plan.
2. Run it without `--yes` and answer the question; try `n` once, then `y`.
3. Run it again: a repeat must change nothing.
4. Run the bootstrap with `--update` (`-Update`).
5. Open a new shell and check that `legion` is on PATH.

Watch for:
- Every question is visible before it waits for input, also when the script is piped.
- Progress during downloads and uv steps; silence of more than about 10 seconds is a finding.
- One plain result line, and exit codes: 0 for done or declined, non-zero for a real failure.
- Files left: the shell profile or user PATH change, `~/.legion`, the uv tool folder.
- Time every step.
