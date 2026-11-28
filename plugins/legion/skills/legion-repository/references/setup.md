# Setup script

For tool operations, read `skills/legion-<agent.tool>/SKILL.md` using the selected account's `agent.tool`; stop if it is missing.

Check the mode using [layout.md](layout.md) first. A `.legion/.git` folder or file means Private; otherwise `git ls-files .legion` must list tracked files for Shared. If neither check matches, setup is not finished: ask the owner the setup choice again, with Private as the default, and complete it.

One run does the whole fresh setup, `--mode shared` for the other choice (Private is the default):

```text
legion repository setup --repo <repo> [--mode shared] [--gate '<command>'] [--rules-file <file>]
```

It creates `project.json`, `workflow.md` with the rules block (Private) or the rules file block (Shared), the script index, handoff, history and local docs from the templates in this folder, applies the ignore rules in [layout.md](layout.md), and commits only the setup files it wrote or left pending, plus `.gitignore` and the rules file (Private in the inner repository); the owner's other files stay out. `--gate` also writes `check-task.ps1` and its index row. Existing files stay byte-identical; a partial setup prints a `skip:` line for each one it keeps. A setup path that has both staged and unstaged changes stops setup with a one-line message; it never commits the unstaged part, and the owner's untracked files stay out. It stops before any change when a file it must edit has uncommitted edits, and asks for `--rules-file` when two rules files are ambiguous. It validates `project.json`; a repeat prints `skip: <Mode> orchestration (already done)`.

Plans and specs use the paths recorded in `workflow.md`. Create `.legion/state/roadmap/<goal-id>.md` from [roadmap-template.md](roadmap-template.md) only after the owner approves a roadmap. New scratch output belongs in `.legion/temp/`. Follow [takeover.md](../../legion-overseer/references/takeover.md) for later state commits.

Dismiss: `legion dismiss [--yes]` inside the repository. It prints each trace it removes and `.legion/`, asks `Apply? [y/N]` (closed input means no), removes exactly what setup wrote (Private: the `.legion/` line in `.git/info/exclude`; Shared: the `.gitignore` lines and the rules file section, left uncommitted), lane worktrees under `.legion/temp/`, then `.legion/`, and ends with `Dismissed: legion is released from this repository.` Private warns first that its history goes with `.legion/`. It refuses in a member session or while another live session holds the overseer lease; without legion it prints `skip: legion in this repository (already dismissed)`.
