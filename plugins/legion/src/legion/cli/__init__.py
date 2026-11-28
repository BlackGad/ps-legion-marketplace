import io
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Optional

import click

from legion.core import _home, _plugin


def _show_version(context: click.Context, _parameter: click.Parameter, value: bool) -> None:
    if not value:
        return
    from legion.core._settings_models import PluginManifest

    manifest = PluginManifest.model_validate_json((_plugin.PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    click.echo(f"{context.find_root().info_name} {manifest.version}")
    context.exit()


# Flags before any verb (`legion --no-channel`) belong to the Python overseer launch.
class _LegionGroup(click.Group):
    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple[Optional[str], Optional[click.Command], list[str]]:
        if args and args[0].startswith("-"):
            return "", _launch_command(""), args
        verb, command, remaining = super().resolve_command(ctx, args)
        ctx.meta["lifecycle_args"] = remaining
        return verb, command, remaining


@click.group(cls=_LegionGroup, invoke_without_command=True, context_settings={"ignore_unknown_options": True}, help="Legion commands")
@click.option("--version", is_flag=True, is_eager=True, expose_value=False, callback=_show_version, help="Show the version and exit.")
@click.option("--home", type=click.Path(path_type=Path), help="Use this folder instead of the user's home folder.")
@click.pass_context
def main(context: click.Context, home: Optional[Path]) -> None:
    # A pipe on Windows defaults to the ANSI code page; messages carry any text.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    # A home may be given with ".." parts; keep one clean absolute path.
    if home is not None:
        home = Path(os.path.normpath(home)).absolute()
    context.obj = home
    verb = context.invoked_subcommand
    args = context.meta.get("lifecycle_args", ())
    skip = verb in ("mcp", "run", "lease", "inbox", "telegram", "validate", "uninstall")
    skip = skip or any(flag in args for flag in ("--help", "--json", "--dry-run")) or (verb == "update" and "--check" in args)
    skip = skip or (verb == "update" and "--self" in args)
    if not skip:
        from legion.services.lifecycle import _lifecycle

        resolved = _home.legion_home(home)
        updating = verb == "update"
        if not updating or not _lifecycle.update_blocked(resolved):
            _lifecycle.first_run(resolved)
    if context.invoked_subcommand is None:
        _report_overseer(context, Path.cwd())


def _report_overseer(
    context: click.Context,
    repo: Path,
    *,
    account: Optional[str] = None,
    tool: Optional[str] = None,
    channels: tuple[str, ...] = (),
    no_channel: bool = False,
    arguments: tuple[str, ...] = (),
    pick: bool = False,
) -> None:
    from legion.services.accounts._accounts import AccountError
    from legion.services.accounts._launchers import LauncherError
    from legion.services.channels import _overseer
    from legion.services.channels._channel import ChannelError
    from legion.services.repository._repository import RepositoryError
    from legion.core._settings_io import SettingsError

    try:
        context.exit(_overseer.run(repo, _home.legion_home(context.obj), account=account, tool=tool, channels=channels, no_channel=no_channel, arguments=arguments, pick=pick))
    except (AccountError, ChannelError, LauncherError, RepositoryError, SettingsError, _overseer.OverseerError, OSError, ValueError) as error:
        click.echo(" ".join(str(error).split()), err=True)
        context.exit(1)


def _launch_command(verb: str) -> click.Command:
    @click.command(verb, help="Choose which account leads the overseer" if verb else "Start the overseer")
    @click.option("--account")
    @click.option("--tool")
    @click.option("--channel", "channels", multiple=True)
    @click.option("--no-channel", is_flag=True)
    @click.option("--repo", type=click.Path(path_type=Path))
    @click.option("--argument", "arguments", multiple=True, help="A literal tool argument; repeat for each word (use --argument=--flag for flags).")
    @click.pass_context
    def launch(context: click.Context, account: Optional[str], tool: Optional[str], channels: tuple[str, ...], no_channel: bool, repo: Optional[Path], arguments: tuple[str, ...]) -> None:
        _report_overseer(context, repo=repo or Path.cwd(), account=account, tool=tool, channels=channels, no_channel=no_channel, arguments=arguments, pick=verb == "pick")

    return launch


@main.command("validate", help="Check a settings or project file against its schema")
@click.option("--kind", required=True, type=click.Choice(["personalisation", "project"]))
@click.option("--path", "path", required=True, type=click.Path(path_type=Path))
@click.pass_context
def validate(context: click.Context, kind: str, path: Path) -> None:
    from legion.core import _validate

    issues = _validate.validate(kind, path)
    for issue in issues:
        click.echo(issue)
    context.exit(1 if issues else 0)


@main.command("update", help="Check for updates, or update legion to the newest version of its track")
@click.option("--check", is_flag=True, help="Check for updates without installing.")
@click.option("--force", is_flag=True, help="Check again even if today's result is cached; requires --check.")
@click.option("--source", default="BlackGad/ps-legion-marketplace")
@click.option("--ref", type=click.Choice(["release", "main"]), default="release")
@click.option("--dry-run", is_flag=True)
@click.option("--yes", is_flag=True, help="Confirm the update without prompting.")
@click.option("--self", "self_update", is_flag=True, help="Update from this overseer session and resume it.")
@click.option("--session-id", help="Current Copilot conversation ID for --self.")
@click.pass_context
def update(context: click.Context, check: bool, force: bool, source: str, ref: str, dry_run: bool, yes: bool, self_update: bool, session_id: str | None) -> None:
    if force and not check:
        raise click.UsageError("--force requires --check")
    if session_id is not None and not self_update:
        raise click.UsageError("--session-id requires --self")
    if self_update:
        if check or dry_run or not yes:
            raise click.UsageError("--self requires --yes and cannot be combined with --check or --dry-run")
        from legion.services.lifecycle import _self_update

        launched_home = os.environ.get("LEGION_HOME")
        home = context.obj or (_home.full_path(Path(launched_home)) if launched_home else None)
        try:
            code = _self_update.request(_home.legion_home(home), source, ref, session_id, source_explicit=context.get_parameter_source("source") == click.core.ParameterSource.COMMANDLINE)
        except _self_update.ERRORS as error:
            raise click.ClickException(str(error)) from None
        context.exit(code)
    if check:
        for flag in ("yes", "dry_run", "source", "ref"):
            if context.get_parameter_source(flag) == click.core.ParameterSource.COMMANDLINE:
                raise click.UsageError(f"--{flag.replace('_', '-')} cannot be used with --check")
        from legion.services.installation import _update

        for line in _update.check_lines(_plugin.PLUGIN_ROOT, context.obj, force=force):
            click.echo(line)
        return
    _run_lifecycle(context, "update", yes, source=source, ref=ref, dry_run=dry_run)


def _run_lifecycle(
    context: click.Context,
    intent: Literal["install", "update", "setup", "uninstall", "purge"],
    yes: bool,
    *,
    source: str = "BlackGad/ps-legion-marketplace",
    ref: str = "release",
    dry_run: bool = False,
    account_tool: str | None = None,
    account_folder: str | None = None,
    account_id: str | None = None,
    knowledge: bool | None = None,
) -> None:
    from legion.services.lifecycle import _lifecycle

    home = _home.legion_home(context.obj)
    if intent in ("uninstall", "purge"):
        click.echo("Checking Legion installations...")
    try:
        with _lifecycle.saved_path():
            changes = _lifecycle.plan(
                home,
                intent,
                source=source,
                source_explicit=context.get_parameter_source("source") == click.core.ParameterSource.COMMANDLINE,
                ref=ref,
                dry_run=dry_run,
                account_tool=account_tool,
                account_folder=account_folder,
                account_id=account_id,
                knowledge=knowledge,
            )
            code = _lifecycle.apply(changes, yes=yes)
    except _lifecycle.ERRORS as error:
        click.echo(" ".join(str(error).split()), err=True)
        context.exit(1)
    context.exit(code)


def _report_lines(context: click.Context, change: Callable[[], list[str]], errors: tuple[type[Exception], ...]) -> None:
    try:
        lines = change()
    except errors as error:
        click.echo(" ".join(str(error).split()), err=True)
        context.exit(1)
    for line in lines:
        click.echo(line)


@main.command("install", help="Install legion for available coding CLIs")
@click.option("--source", default="BlackGad/ps-legion-marketplace")
@click.option("--ref", type=click.Choice(["release", "main"]), default="release")
@click.option("--dry-run", is_flag=True)
@click.option("--yes", is_flag=True)
@click.pass_context
def install(context: click.Context, source: str, ref: str, dry_run: bool, yes: bool) -> None:
    _run_lifecycle(context, "install", yes, source=source, ref=ref, dry_run=dry_run)


@main.command("uninstall", help="Remove legion. Use --purge to also delete settings, notes and logins.")
@click.option("--purge", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--yes", is_flag=True)
@click.pass_context
def uninstall(context: click.Context, purge: bool, dry_run: bool, yes: bool) -> None:
    _run_lifecycle(context, "purge" if purge else "uninstall", yes, dry_run=dry_run)


@main.command("mcp", help="Serve read-only status and explicit-account limits over MCP stdio")
@click.pass_context
def mcp_command(context: click.Context) -> None:
    try:
        from legion.mcp.server import main as serve
    except ModuleNotFoundError as error:
        if error.name != "mcp":
            raise
        click.echo("MCP dependencies are missing; run legion setup.", err=True)
        context.exit(1)
    context.exit(serve(context.obj))


@main.command("status", help="Show accounts, commands, channels and this repository")
@click.option("--json", "as_json", is_flag=True, help="Print the typed status report as JSON.")
@click.pass_obj
def status(home: Optional[Path], as_json: bool) -> None:
    from legion.services.status import _status
    from legion.services.status import status as status_report

    report = status_report(Path.cwd(), home)
    if as_json:
        click.echo(report.model_dump_json())
        return
    for line in _status.status_lines(report):
        click.echo(line)


@main.command(
    "limits",
    help=(
        "Show each account's usage windows and reset times. Each named account runs a real minimal ping through its own program, consuming quota and billable on paid accounts. "
        "A ping creates a disposable folder; Codex leaves a session record. Foundry runs az account get-access-token instead. Each account can take up to 120 seconds. "
        "A bare text call selects all accounts. JSON and the MCP tool require explicit --id values."
    ),
)
@click.option("--id", "accounts", multiple=True, help="Account to ping; repeat for each selected account.")
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=120.0, show_default=True, help="Maximum seconds per account.")
@click.option("--json", "as_json", is_flag=True, help="Print typed limits as JSON; requires explicit --id values.")
@click.pass_context
def limits(context: click.Context, accounts: tuple[str, ...], timeout: float, as_json: bool) -> None:
    if not math.isfinite(timeout):
        raise click.BadParameter("must be finite", param_hint="--timeout")
    from pydantic import TypeAdapter

    from legion.services.limits.limit_probes import LimitsError, report_lines
    from legion.services.limits import AccountLimits, configured_accounts, limits as limits_report

    try:
        # A bare text call lists every account; --json and the MCP tool name their accounts.
        if not accounts and not as_json:
            accounts = tuple(agent.id for agent in configured_accounts(context.obj))
            if not accounts:
                click.echo("No accounts yet; run legion add.")
                return
        reports = limits_report(context.obj, accounts, timeout)
    except LimitsError as error:
        click.echo(str(error), err=True)
        context.exit(1)
    if as_json:
        click.echo(TypeAdapter(list[AccountLimits]).dump_json(reports).decode("utf-8"))
        return
    for line in report_lines(reports):
        click.echo(line)


@main.group(help="Send or read repository overseer messages")
def inbox() -> None:
    pass


@inbox.command("send", help="Send a message to a repository overseer")
@click.option("--to", "target", required=True, type=click.Path(path_type=Path))
@click.option("--from", "sender", help="the sender's repository path (default: current folder)")
@click.option("--text", required=True)
@click.pass_obj
def inbox_send(home: Optional[Path], target: Path, sender: Optional[str], text: str) -> None:
    from legion.services.channels import _inbox

    sender = sender or str(Path.cwd().resolve())
    if sender.strip().lower() == "telegram":
        raise click.UsageError("--from telegram is reserved for the Telegram bridge")
    if not target.is_dir():
        raise click.UsageError(f"--to {target} is not an existing folder")
    _inbox.send(target, sender, text, home)


@inbox.command("read", help="Read this repository's inbox")
@click.option("--repo", type=click.Path(path_type=Path), default=None)
@click.pass_obj
def inbox_read(home: Optional[Path], repo: Optional[Path]) -> None:
    from legion.services.channels import _inbox

    _inbox.read(repo or Path.cwd(), home)


@main.group(help="Send to Telegram or bridge its messages into the inbox")
def telegram() -> None:
    pass


@telegram.command("send", help="Send a message or files through a channel's bot")
@click.option("--channel", required=True)
@click.option("--chat", required=True)
@click.option("--text", required=True)
@click.option("--format", "text_format", type=click.Choice(["text", "markdownv2"]), default="text")
@click.option("--reply-to", type=int)
@click.option("--file", "files", multiple=True, type=click.Path(path_type=Path))
@click.pass_context
def telegram_send(context: click.Context, channel: str, chat: str, text: str, text_format: str, reply_to: Optional[int], files: tuple[Path, ...]) -> None:
    from legion.services.channels import _telegram

    context.exit(_telegram.send(channel, chat, text, text_format, reply_to, list(files), home=context.obj))


@telegram.command("react", help="React to a message")
@click.option("--channel", required=True)
@click.option("--chat", required=True)
@click.option("--message", required=True)
@click.option("--emoji", required=True)
@click.pass_context
def telegram_react(context: click.Context, channel: str, chat: str, message: str, emoji: str) -> None:
    from legion.services.channels import _telegram

    context.exit(_telegram.react(channel, chat, message, emoji, home=context.obj))


@telegram.command("bridge", help="Poll a channel's bot and write owner messages into the repository inbox")
@click.option("--channel", required=True)
@click.option("--repo", required=True, type=click.Path(path_type=Path))
@click.pass_context
def telegram_bridge(context: click.Context, channel: str, repo: Path) -> None:
    from legion.services.channels import _bridge

    context.exit(_bridge.run(channel, repo, home=context.obj))


@main.group(help="Take, check or release this repository's overseer lease")
def lease() -> None:
    pass


_repo_option = click.option("--repo", type=click.Path(path_type=Path), default=None, help="the repository (default: current folder)")
_process_option = click.option("--process-id", type=int, help="the owner session's process id (default: the agent session that runs legion)")


# Prints the lease message, or its refusal on stderr with exit code 1.
def _report_lease(context: click.Context, change: Callable[[], str]) -> None:
    from legion.services.repository._lease import LeaseError

    try:
        click.echo(change())
    except LeaseError as error:
        click.echo(str(error), err=True)
        context.exit(1)


@lease.command("take", help="Take the lease for this session; a stale lease is replaced")
@_repo_option
@click.option("--tool", required=True)
@click.option("--account", required=True)
@_process_option
@click.pass_context
def lease_take(context: click.Context, repo: Optional[Path], tool: str, account: str, process_id: Optional[int]) -> None:
    from legion.services.repository import _lease_take

    _report_lease(context, lambda: _lease_take.take_for_session(repo or Path.cwd(), tool, account, process_id))


@lease.command("status", help="Print free, held, stale or foreign")
@_repo_option
def lease_status(repo: Optional[Path]) -> None:
    from legion.services.repository import _lease

    click.echo(_lease.state(repo or Path.cwd()).state)


@lease.command("release", help="Release the lease if this session holds it")
@_repo_option
@_process_option
@click.pass_context
def lease_release(context: click.Context, repo: Optional[Path], process_id: Optional[int]) -> None:
    from legion.services.repository import _lease

    _report_lease(context, lambda: _lease.release(repo or Path.cwd(), process_id))


@main.command("dismiss", help="Release this repository from the legion: remove what setup added, then .legion/")
@click.option("--yes", is_flag=True, help="Apply without asking.")
@click.pass_context
def dismiss(context: click.Context, yes: bool) -> None:
    from legion.services.repository import _dismiss
    from legion.services.repository._repository import RepositoryError

    def ask() -> bool:
        try:
            return click.prompt("Apply? [y/N]", default="", show_default=False, prompt_suffix=" ").strip().lower() in ("y", "yes")
        except click.Abort:
            return False

    try:
        _dismiss.dismiss(Path.cwd(), yes, ask, click.echo)
    except (RepositoryError, OSError) as error:
        click.echo(" ".join(str(error).split()), err=True)
        context.exit(1)


@main.group(help="Set up this repository for legion orchestration")
def repository() -> None:
    pass


@repository.command("setup", help="Create and commit Private or Shared orchestration files")
@_repo_option
@click.option("--mode", type=click.Choice(["private", "shared"], case_sensitive=False), default="private")
@click.option("--gate")
@click.option("--rules-file")
@click.pass_context
def repository_setup(context: click.Context, repo: Optional[Path], mode: Literal["private", "shared"], gate: Optional[str], rules_file: Optional[str]) -> None:
    from legion.services.repository import _repository

    _report_lines(context, lambda: _repository.setup(repo or Path.cwd(), mode, gate, rules_file), (_repository.RepositoryError,))


@main.group(help="Add, remove or check the owner's channel bindings")
def channel() -> None:
    pass


def _report_channel(context: click.Context, change: Callable[[], list[str]]) -> None:
    from legion.services.channels._channel import ChannelError
    from legion.core._settings_io import SettingsError

    _report_lines(context, change, (ChannelError, SettingsError))


_channel_kind_option = click.option("--kind", required=True, help="the channel kind, such as telegram")
_channel_accounts_option = click.option("--account", "accounts", required=True, multiple=True, help="an account id; repeat for each account")


@channel.command("add", help="Bind a channel to accounts")
@click.option("--id", "channel_id", required=True)
@_channel_kind_option
@click.option("--target", required=True, help="the destination, such as a Telegram chat id")
@_channel_accounts_option
@click.pass_context
def channel_add(context: click.Context, channel_id: str, kind: str, target: str, accounts: tuple[str, ...]) -> None:
    from legion.services.channels import _channel

    _report_channel(context, lambda: [_channel.add(_home.legion_home(context.obj), channel_id, kind, target, list(accounts))])


@channel.command("remove", help="Remove a channel binding")
@click.option("--id", "channel_id", required=True)
@click.pass_context
def channel_remove(context: click.Context, channel_id: str) -> None:
    from legion.services.channels import _channel

    _report_channel(context, lambda: [_channel.remove(_home.legion_home(context.obj), channel_id)])


@channel.command("check", help="Show what a channel needs for each account, without starting any CLI")
@_channel_kind_option
@_channel_accounts_option
@click.pass_context
def channel_check(context: click.Context, kind: str, accounts: tuple[str, ...]) -> None:
    from legion.services.channels import _channel

    _report_channel(context, lambda: _channel.check(_home.legion_home(context.obj), kind, list(accounts)))


@main.group(help="Add, register, rename or remove accounts")
def account() -> None:
    pass


def _report_account(context: click.Context, change: Callable[[], list[str]]) -> None:
    from legion.services.accounts._accounts import AccountError
    from legion.services.repository._footprints import FootprintError
    from legion.services.accounts._launchers import LauncherError
    from legion.core._settings_io import SettingsError

    _report_lines(context, change, (AccountError, FootprintError, LauncherError, SettingsError, ValueError, OSError))


_account_id_option = click.option("--id", "account_id", required=True, help="the account id")
_account_tool_option = click.option("--tool", "tool_id", required=True, help="the tool, such as claude, codex or copilot")


@account.command("add", help="Add a subscription account (a new folder, or a copy of an existing one), or change the command of an existing one.")
@_account_id_option
@_account_tool_option
@click.option("--command", help="the account's own CLI program, when it is not the tool's stock command")
@click.option("--config-dir", help="an existing folder to copy into the new account")
@click.option("--provider", type=click.Choice(["foundry"]))
@click.option("--endpoint")
@click.option("--deployment", "deployments", multiple=True)
@click.option("--default", "default_model")
@click.option("--alias", "aliases", multiple=True)
@click.option("--standard", is_flag=True)
@click.option("--list", "list_only", is_flag=True)
@click.pass_context
def account_add(
    context: click.Context,
    account_id: str,
    tool_id: str,
    command: Optional[str],
    config_dir: Optional[str],
    provider: Optional[str],
    endpoint: Optional[str],
    deployments: tuple[str, ...],
    default_model: Optional[str],
    aliases: tuple[str, ...],
    standard: bool,
    list_only: bool,
) -> None:
    from legion.services.accounts import _accounts

    if standard and (aliases or default_model):
        raise click.UsageError("--standard cannot be combined with --alias or --default")
    if standard and provider != "foundry":
        raise click.UsageError("--standard is only for Foundry add or models")
    if provider == "foundry":
        if config_dir:
            raise click.UsageError("--config-dir is for existing subscription accounts; omit it for Foundry.")
        if not endpoint:
            raise click.UsageError("--endpoint is required for Foundry")
        _report_account(context, lambda: _accounts.add_foundry(_home.legion_home(context.obj), account_id, tool_id, endpoint, list(deployments), default_model, list(aliases), standard, list_only))
        return
    _report_account(context, lambda: _accounts.add_subscription(_home.legion_home(context.obj), account_id, tool_id, command, config_dir, click.echo))


@account.command("models", help="Show or change a Foundry account's deployments")
@_account_id_option
@click.option("--deployment", "deployments", multiple=True)
@click.option("--default", "default_model")
@click.option("--alias", "aliases", multiple=True)
@click.option("--standard", is_flag=True)
@click.option("--list", "list_only", is_flag=True)
@click.pass_context
def account_models(context: click.Context, account_id: str, deployments: tuple[str, ...], default_model: Optional[str], aliases: tuple[str, ...], standard: bool, list_only: bool) -> None:
    from legion.services.accounts import _accounts

    if standard and (aliases or default_model):
        raise click.UsageError("--standard cannot be combined with --alias or --default")
    selected_aliases = [] if aliases == ("",) else list(aliases) if aliases else None
    _report_account(context, lambda: _accounts.models(_home.legion_home(context.obj), account_id, list(deployments), default_model, selected_aliases, standard, list_only))


@main.command("setup", help="Finish setup: settings, commands, legion in every account")
@click.option("--yes", is_flag=True, help="Confirm the Legion setup plan.")
@click.option("--knowledge/--no-knowledge", default=None, help="Include the llm-wiki choice in the setup plan.")
@click.pass_context
def setup(context: click.Context, yes: bool, knowledge: Optional[bool]) -> None:
    _run_lifecycle(context, "setup", yes, knowledge=knowledge)


@main.command("add", help="Add a claude, codex or copilot account (a new folder, or a copy of an existing one)")
@click.argument("tool")
@click.argument("folder", required=False)
@click.option("--yes", is_flag=True, help="Apply without asking.")
@click.option("--id", "account_id", hidden=True, help="the account id, for one add")
@click.option("--source", hidden=True, help="where the plugin installer takes legion from")
@click.pass_context
def add(context: click.Context, tool: str, folder: Optional[str], yes: bool, account_id: Optional[str], source: Optional[str]) -> None:
    tool_id, separator, inline_folder = tool.partition(":")
    if separator:
        folder = inline_folder if folder is None else f"{inline_folder}:{folder}"
    _run_lifecycle(context, "setup", yes, source=source or "BlackGad/ps-legion-marketplace", account_tool=tool_id, account_folder=folder, account_id=account_id)


@main.group(help="Check and install the CLI tools legion needs")
def tool() -> None:
    pass


@tool.command("install", help="Show what installing a tool would do; with --yes, install it")
@click.option("--tool", "tool_id", required=True, help="the tool, such as claude, git or uv")
@click.option("--yes", is_flag=True, help="the owner agreed: run the install")
@click.option("--method", type=click.Choice(["winget", "msi", "zip"]), help="Windows only: how to install")
@click.pass_context
def tool_install(context: click.Context, tool_id: str, yes: bool, method: Optional[str]) -> None:
    from legion.services.installation._tool_install import ToolInstaller, ToolInstallError, current_platform

    try:
        context.exit(ToolInstaller(_home.legion_home(context.obj), current_platform(), yes, click.echo).install(tool_id, method))
    except (ToolInstallError, ValueError, OSError) as error:
        click.echo(" ".join(str(error).split()), err=True)
        context.exit(1)


# Starts an account's program with its config folder: `legion run --account <id> -- <tool arguments>`.
@main.command("run", hidden=True, context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("--account", required=True)
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run_command(context: click.Context, account: str, arguments: tuple[str, ...]) -> None:
    from legion.services.accounts._accounts import AccountError
    from legion.services.accounts._launch import run_account
    from legion.services.accounts._launchers import LauncherError
    from legion.core._settings_io import SettingsError

    try:
        context.exit(run_account(account, list(arguments), _home.legion_home(context.obj)))
    except (AccountError, LauncherError, SettingsError, ValueError, OSError) as error:
        click.echo(" ".join(str(error).split()), err=True)
        context.exit(1)


@account.command("rename", help="Give an account a new id; its folder stays")
@_account_id_option
@click.option("--new-id", required=True, help="the new account id")
@click.pass_context
def account_rename(context: click.Context, account_id: str, new_id: str) -> None:
    from legion.services.accounts import _accounts

    _report_account(context, lambda: _accounts.rename(_home.legion_home(context.obj), account_id, new_id))


@main.command("remove", help="Remove an account (its folder is kept)")
@_account_id_option
@click.pass_context
def remove(context: click.Context, account_id: str) -> None:
    from legion.services.accounts import _accounts

    _report_account(context, lambda: _accounts.remove(_home.legion_home(context.obj), account_id))


main.add_command(_launch_command("pick"))


@main.command("help", help="Show the legion commands")
@click.pass_context
def help_command(context: click.Context) -> None:
    click.echo(context.parent.get_help() if context.parent else "")
