# Repository instructions

Shared instructions for every coding agent working in this repository (Claude Code reads them through `CLAUDE.md`). The user's explicit instructions take precedence.

## What this repository is

A plugin marketplace with one plugin, `legion`: orchestration as skills for Claude Code, Codex CLI and Copilot CLI.

| Path | Holds |
| --- | --- |
| `.claude-plugin/`, `.agents/plugins/` | Marketplace manifests (Claude Code and Copilot; Codex) |
| `plugins/legion/skills/` | Skills: `SKILL.md` plus `references/` per skill |
| `plugins/legion/src/legion/` | Python package: `core/`, `services/`, `cli/`, `hook/`; project and lockfile in `plugins/legion/` |
| `plugins/legion/hooks/` | Session-start hooks for all three tools |
| `plugins/legion/schemas/` | JSON schemas for personalisation and project files |
| `install.ps1`, `install.sh` | Root bootstraps: they only obtain uv, then run the Python `legion install` with the same options; `install.ps1` runs on Windows PowerShell 5.1 and 7 |
| `docs/reference.md` | Installer options, CLI guides and technical details |
| `tests/repository/` | Temporary PowerShell bootstrap tests and runner support under `tests/` |
| `tests/python/` | Python tests, including repository checks under `tests/python/repository/` |
| `pyproject.toml` | Repository settings for Ruff, Pyright, pytest and Coverage.py, outside the client plugin |
| `tools/` | Python maintenance scripts, including `generate.py` for the tools index and editor schemas |

## Rules

A settings field exists only if a script reads it. Everything else is a plain-words preference in the owner's `legion-notes.md`, which agents read. Do not add a property for each decision.

- **Legion runs in Python.** The only PowerShell runtime script is the Windows bootstrap `install.ps1` (5.1 and 7). Its bootstrap tests and their support scripts remain temporarily.
- **Safe to run again.** Every script checks before it acts; a repeat prints `skip: <what> (already done)` and changes nothing.
- **Ask before installing.** Install commands are shown as hints and run only after the owner's yes (`-Yes`).
- **No secrets, no personal data.** Never store or print tokens or keys. Never commit user names, host names, local paths, endpoints, chat ids or account names from a real setup; use neutral examples (`codex-a`, `my-project`, `https://<resource>.cognitiveservices.azure.com`).
- **Readable and simple code is a must (KISS).** Write code the next person can read without explanation: plain control flow, clear names, no clever tricks, no abstraction without a real second use.
- **DRY is desired.** Keep one home for each rule, format or list and reuse it, unless repeating it is simpler to read.
- **Minimal diff.** Change only what the task needs.
- **Docs always in sync.** Update `README.md`, `docs/reference.md` and affected skills and references in the same pull request when behavior, commands, files or settings change.
- Resolve paths with `resolve_directory` and compare them with `same_path` in `plugins/legion/src/legion/core/_home.py`. Keep deliberate link-refusal checks lexical.
- Skills talk to the owner in plain words. `SKILL.md` holds the flow and the conversation; commands, script paths and flags go in `references/`.

## Release

Versions and the changelog are made by the release workflow. Task branches and pull requests never change `version` in the three `plugin.json` files and never edit `CHANGELOG.md`. The release workflow keeps one release pull request open; the owner merges it to tag `vX.Y.Z` and publish the GitHub Release. The overseer may add a short "What is new" summary to the GitHub Release notes after the tag is published, and never edits the release pull request body.

## Tests

Tests never call real `az`, `claude`, `codex`, `copilot`, winget or the network. Use the fakes in `tests/` and replace download functions.

Python uses uv only (Python >=3.12). The root `pyproject.toml` holds the tool settings, including standard-mode Pyright. From the repository root, set `UV_PROJECT_ENVIRONMENT` to an absolute path outside the plugin cache, such as `(Join-Path $PWD '.legion/temp/python/env')`. Before pytest, set `LEGION_TEST_PWSH` to the absolute pwsh path, such as `(Get-Command pwsh).Source`.

```powershell
uv run --project plugins/legion --frozen --extra mcp --group dev ruff check plugins/legion/src tests/python tests/integration tools tests/playground
uv run --project plugins/legion --frozen --extra mcp --group dev ruff format --check plugins/legion/src tests/python tests/integration tools tests/playground
uv run --project plugins/legion --frozen --extra mcp --group dev pyright --project pyproject.toml
uv run --project plugins/legion --frozen --extra mcp --group dev pytest
```

Run the remaining bootstrap tests through their PowerShell runner. The last line must be `<n> passed, 0 failed`.

```powershell
pwsh -NoProfile -File tests/run.ps1
```

CI runs the test gates on Windows, macOS and Linux and parses `install.ps1` with Windows PowerShell 5.1. The test gate and the integration suite (`tests/integration/run.py`) are required checks.

## Not committed

- Design specs, implementation plans and verification notes (`docs/specs/`, `docs/plans/`, `docs/superpowers/`, `docs/verification/`) are temporary working documents; they stay local and are ignored.
- Legion scratch, logs, reports and worktrees go in `.legion/temp/` (ignored).

## Git

- Work on a feature branch; `main` changes only through a pull request the owner approves.
- Never force-push or amend.
- Commit messages and pull request titles are conventional commit subjects (`feat: ...`, `fix(install): ...`, `docs: ...`, `chore: ...`). The squash merge uses the pull request title as the commit on `main`, and the release workflow builds the changelog from it.
- Add no `Co-Authored-By` trailer for any agent or tool, and no "Generated with" footer in pull request descriptions.
