"""`pil`: keep prompts in a library and run them without ceremony."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn, TextIO

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.prompt import Prompt as Ask
from typer.core import TyperCommand, TyperGroup

from . import __version__, config, fill, render, runner
from .clipboard import ClipboardError, read_clipboard, write_clipboard
from .examples import EXAMPLES
from .kinds import DEFAULT_KIND, KINDS, Output, Runner
from .store import (
    Ambiguous,
    Library,
    NotFound,
    Prompt,
    PromptError,
    Variable,
    matches,
    parse,
)
from .template import Value

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

ROOT_FLAGS = {"-h", "--help", "-V", "--version", "--install-completion", "--show-completion"}


def _library() -> Library:
    return Library(config.prompts_dir())


def _complete_names(incomplete: str) -> list[tuple[str, str]]:
    try:
        prompts = _library().all()
    except OSError:
        return []
    # A shell only keeps candidates that start with what was typed, so offer the short
    # form of a name when that's what is being typed.
    titles = [prompt.title for prompt in prompts]
    found = []
    for prompt in prompts:
        note = prompt.description or prompt.kind.summary
        if prompt.name.startswith(incomplete):
            found.append((prompt.name, note))
        elif prompt.title.startswith(incomplete) and titles.count(prompt.title) == 1:
            found.append((prompt.title, note))
    return found


class PilGroup(TyperGroup):
    """Treats `pil NAME …` as `pil run NAME …`, so running a prompt is the shortest command."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in ROOT_FLAGS:
            args = ["run", *args]
        return super().parse_args(ctx, args)

    def shell_complete(self, ctx: Any, incomplete: str) -> list[Any]:
        from typer._click.shell_completion import CompletionItem

        items = super().shell_complete(ctx, incomplete)
        if not incomplete.startswith("-"):
            items += [CompletionItem(name, help=note) for name, note in _complete_names(incomplete)]
        return items


class RunCommand(TyperCommand):
    """Completes a prompt's own variables once the prompt has been named."""

    def shell_complete(self, ctx: Any, incomplete: str) -> list[Any]:
        from typer._click.shell_completion import CompletionItem

        name = ctx.params.get("name")
        if not name:
            return super().shell_complete(ctx, incomplete)
        try:
            variables = _library().resolve(name).variables()
        except (PromptError, OSError):
            return []
        flags = []
        for variable in variables:
            if variable.type == "toggle":
                if not variable.default:
                    flags.append((f"--{variable.name}", variable))
                if variable.default is not False:
                    flags.append((f"--no-{variable.name}", variable))
            elif variable.type == "choice":
                flags += [(f"--{variable.name}={option}", variable) for option in variable.options]
            else:
                flags.append((f"--{variable.name}=", variable))
        return [
            CompletionItem(flag, help=variable.help)
            for flag, variable in flags
            if flag.startswith(incomplete)
        ]


app = typer.Typer(
    cls=PilGroup,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)


def _fail(
    message: str, *, detail: str | None = None, hint: str | None = None, code: int = 1
) -> NoReturn:
    render.error(err, message, detail=detail, hint=hint)
    raise typer.Exit(code)


def _find(name: str) -> Prompt:
    try:
        return _library().resolve(name)
    except Ambiguous as e:
        _fail(str(e), hint="Which one? " + ", ".join(f"[bold]{match}[/]" for match in e.matches))
    except NotFound as e:
        if e.close:
            hint = "Did you mean " + " or ".join(f"[bold]{match}[/]" for match in e.close) + "?"
        else:
            hint = "See what's in your library with [bold]pil ls[/]."
        _fail(str(e), hint=hint)


def _config() -> config.Config:
    try:
        return config.load()
    except config.ConfigError as e:
        _fail(str(e))


# ── Input ─────────────────────────────────────────────────────────────────────


class InputError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def _stdin_is_piped(stdin: TextIO) -> bool:
    """True when stdin is a pipe or redirected file, not a terminal or /dev/null."""
    try:
        mode = os.fstat(stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or stat.S_ISSOCK(mode)


def _read(stdin: TextIO) -> str:
    buffer = getattr(stdin, "buffer", None)
    if buffer is not None:
        return buffer.read().decode("utf-8", errors="replace")
    return stdin.read()


def resolve_input(file: Path | None, stdin: TextIO | None = None) -> tuple[str, str]:
    """Work out what a prompt should act on: a file, piped stdin, or else the clipboard.

    Returns (text, origin) where origin is a short description for display.
    """
    stdin = stdin or sys.stdin
    if file is not None:
        if str(file) == "-":
            return _read(stdin), "stdin"
        try:
            return file.read_text(encoding="utf-8", errors="replace"), file.name
        except OSError as e:
            raise InputError(f"Couldn't read {file}: {e.strerror or e}") from e
    if _stdin_is_piped(stdin):
        return _read(stdin), "stdin"
    try:
        return read_clipboard(), "clipboard"
    except ClipboardError as e:
        raise InputError(
            str(e), hint="Pipe text in or name a file instead: [bold]pil -f notes.md NAME[/]"
        ) from e


EMPTY = {
    "clipboard": ("Your clipboard is empty.", "Copy some text first, or pipe it in."),
    "stdin": ("Nothing came through stdin.", None),
}


# ── Running ───────────────────────────────────────────────────────────────────


def _ask_for(prompt: Prompt, wanted: list[Variable], values: dict[str, Value]) -> None:
    """Ask at the terminal for the variables a prompt still needs."""
    err.print(render.badge(prompt))
    for variable in wanted:
        label = f"  [bold {render.ACCENT}]{variable.name}[/]"
        if variable.help:
            label += f" [{render.FAINT}]{variable.help}[/]"
        if variable.type == "toggle":
            label += r" \[y/n]"
        elif variable.options:
            label += rf" \[{'/'.join(variable.options)}]"
        while True:
            answer = Ask.ask(label, console=err)
            if not answer.strip():
                continue
            try:
                values[variable.name] = fill.coerce(variable, answer)
            except fill.FillError as e:
                err.print(f"  [{render.ERROR}]{e}[/]")
                continue
            break


def _deliver(invocation: runner.Invocation, text: str, *, raw: bool, facts: list[str]) -> None:
    """Send a run's result where it's meant to go."""
    prompt = invocation.prompt
    if invocation.output is Output.CLIPBOARD:
        try:
            write_clipboard(text)
        except ClipboardError as e:
            # Don't lose the result just because it couldn't be copied.
            print(text)
            _fail(str(e), hint="The result was printed instead.")
        if invocation.input_text is not None:
            before, after = len(invocation.input_text.split()), len(text.split())
            facts = [f"{before:,} → {render.plural(after, 'word')}", *facts]
            render.done(err, "Clipboard updated", *facts)
        else:
            render.done(err, "Copied to the clipboard", render.plural(len(text.split()), "word"))
    elif out.is_terminal and not raw and prompt.kind.output is Output.TERMINAL:
        out.print(Markdown(text))
    else:
        print(text)


def _launch(argv: list[str]) -> None:
    """Hand the terminal over to Claude Code."""
    if not shutil.which(argv[0]):
        _fail(
            f"Couldn't find the {argv[0]!r} command.",
            hint="Install Claude Code (https://claude.com/claude-code), or point pil at it with "
            "[bold]claude = …[/] in config.toml.",
        )
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(argv[0], argv)


# pil's own options, which are also understood after a prompt's name: (parameter, takes a value).
OWN_OPTIONS = {
    "-n": ("dry_run", False),
    "--dry-run": ("dry_run", False),
    "-r": ("raw", False),
    "--raw": ("raw", False),
    "-f": ("file", True),
    "--file": ("file", True),
    "-o": ("output", True),
    "--output": ("output", True),
    "-m": ("model", True),
    "--model": ("model", True),
}


def own_options(args: list[str], names: set[str]) -> tuple[list[str], dict[str, Any]]:
    """Pick pil's own options out of the arguments that follow a prompt's name.

    A prompt's variables come first: if it has one called `model`, `--model` is that variable.
    """
    rest: list[str] = []
    found: dict[str, Any] = {}
    queue = list(args)
    while queue:
        arg = queue.pop(0)
        if arg == "--":
            rest += [arg, *queue]
            break
        flag, equals, value = arg.partition("=")
        if flag not in OWN_OPTIONS or flag.removeprefix("--") in names:
            rest.append(arg)
            continue
        parameter, takes_value = OWN_OPTIONS[flag]
        if not takes_value:
            found[parameter] = True
            continue
        if not equals:
            if not queue:
                raise fill.FillError(f"{flag} needs a value.")
            value = queue.pop(0)
        found[parameter] = value
    return rest, found


RUN_EPILOG = (
    "[bold]Examples[/]\n\n"
    "  [cyan]pil ticket ABC-123[/]           fill in a prompt and start a session\n"
    "  [cyan]pil ticket ABC-123 --no-tests[/]  …switching one of its toggles off\n"
    "  [cyan]pil fix[/]                      rewrite the clipboard in place\n"
    "  [cyan]git diff | pil explain[/]       …or act on piped text instead\n"
    "  [cyan]pil -n ticket ABC-123[/]        just show the filled-in prompt\n"
    "  [cyan]pil ticket -h[/]                see what a prompt wants\n\n"
    "Options can go either side of the prompt's name. After it, the prompt's own variables "
    "win any clash of names."
)


@app.command(
    cls=RunCommand,
    epilog=RUN_EPILOG,
    context_settings={"allow_interspersed_args": False},
)
def run(
    name: Annotated[
        str,
        typer.Argument(
            help="The prompt to run. The last part of its name or a unique prefix will do.",
            autocompletion=_complete_names,
            show_default=False,
        ),
    ],
    args: Annotated[
        list[str] | None,
        typer.Argument(
            help="Values for the prompt's variables. See them with [bold]pil NAME -h[/].",
            show_default=False,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Print the filled-in prompt instead of running it."),
    ] = False,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            "-f",
            help="Act on a file instead of the clipboard ([bold]-[/] for stdin).",
            show_default=False,
        ),
    ] = None,
    output: Annotated[
        Output | None,
        typer.Option(
            "--output", "-o", help="Send the result here instead of where the prompt says."
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Use this model for the run.", show_default=False),
    ] = None,
    raw: Annotated[
        bool, typer.Option("--raw", "-r", help="Print answers as plain text, not Markdown.")
    ] = False,
) -> None:
    """Run a prompt. [dim]`pil NAME …` is short for `pil run NAME …`.[/]"""
    prompt = _find(name)
    args = args or []
    flags = args[: args.index("--")] if "--" in args else args
    if "-h" in flags or "--help" in flags:
        render.details(out, prompt, body=False)
        return
    if prompt.error:
        _fail(
            f"{prompt.name} can't be run until it's fixed.",
            detail=prompt.error,
            hint=f"Open it with [bold]pil edit {prompt.name}[/].",
        )

    variables = prompt.variables()
    usage = f"[bold]pil {prompt.name}[/] [{render.ACCENT}]{escape(fill.synopsis(variables))}[/]"
    try:
        args, own = own_options(args, {variable.name for variable in variables})
        values = fill.parse_args(variables, args)
    except fill.FillError as e:
        _fail(str(e), hint=usage, code=2)
    dry_run = dry_run or own.get("dry_run", False)
    raw = raw or own.get("raw", False)
    model = own.get("model", model)
    if "file" in own:
        file = Path(own["file"])
    if "output" in own:
        try:
            output = Output(own["output"])
        except ValueError:
            choices = " or ".join(Output)
            _fail(f"--output should be {choices}, not {own['output']!r}.", code=2)
    if output is not None and prompt.kind.runner is Runner.SESSION:
        _fail(
            f"{prompt.name} starts a session, so it has no result to send anywhere.",
            hint="Use [bold]-n[/] to print the filled-in prompt.",
        )
    if output is None and prompt.kind.output is not None and not out.is_terminal:
        # Piped or redirected: whatever is on the other end wants the result, so
        # `pil fix < draft.md > fixed.md` works without touching the clipboard.
        output = Output.TERMINAL

    wanted = fill.missing(variables, values)
    if wanted:
        if not (sys.stdin.isatty() and err.is_terminal):
            names = ", ".join(variable.name for variable in wanted)
            _fail(f"{prompt.name} still needs: {names}.", hint=usage, code=2)
        try:
            _ask_for(prompt, wanted, values)
        except (KeyboardInterrupt, EOFError):
            err.print("[dim]Cancelled.[/]")
            raise typer.Exit(130) from None
    values = fill.with_defaults(variables, values)

    content, origin = _input_for(prompt, file)
    try:
        invocation = runner.prepare(
            prompt,
            values,
            _config(),
            input_text=content,
            output=output,
            overrides={"model": model},
        )
    except runner.RunError as e:
        _fail(str(e), detail=e.detail)
    except ClipboardError as e:
        _fail(str(e))

    if dry_run:
        if invocation.argv:
            session = invocation.runner is Runner.SESSION
            shown = [*invocation.argv[:-1], "…"] if session else invocation.argv
            err.print(f"[{render.FAINT}]$ {escape(shlex.join(shown))}[/]")
        print(invocation.text)
    elif invocation.runner is Runner.SESSION:
        _launch(invocation.argv)
    elif invocation.runner is Runner.NONE:
        _deliver(invocation, invocation.text, raw=True, facts=[])
    else:
        if content is not None:
            err.print(render.source_line(origin, content.strip(), err.width))
        _ask_claude(invocation, raw=raw)


def _input_for(prompt: Prompt, file: Path | None) -> tuple[str | None, str]:
    """The text a prompt acts on and where it came from, if the prompt acts on any."""
    if not prompt.takes_input:
        return None, ""
    try:
        content, origin = resolve_input(file)
    except InputError as e:
        _fail(str(e), hint=e.hint)
    if not content.strip():
        message, hint = EMPTY.get(origin, (f"{origin} is empty.", None))
        _fail(message, hint=hint)
    return content, origin


def _ask_claude(invocation: runner.Invocation, *, raw: bool) -> None:
    """Run a one-shot prompt, with a spinner while Claude thinks."""
    job = runner.Job(invocation)
    try:
        with err.status(render.working(invocation.prompt), spinner_style=render.ACCENT):
            result = job.run()
    except runner.RunError as e:
        _fail(str(e), detail=e.detail, hint=e.hint)
    except KeyboardInterrupt:
        job.cancel()
        err.print("[dim]Cancelled.[/]")
        raise typer.Exit(130) from None
    facts = [f"{result.seconds:.1f}s"]
    if result.model:
        facts.append(result.model)
    _deliver(invocation, runner.settle(invocation, result.text), raw=raw, facts=facts)


# ── Managing the library ──────────────────────────────────────────────────────


@app.command("ls")
def list_prompts(
    query: Annotated[
        list[str] | None,
        typer.Argument(help="Only prompts matching these words.", show_default=False),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option("--kind", "-k", help=f"Only this kind: {', '.join(KINDS)}."),
    ] = None,
    tag: Annotated[
        str | None, typer.Option("--tag", "-t", help="Only prompts with this tag.")
    ] = None,
) -> None:
    """List your prompts. [dim]Piped, it prints tab-separated name, kind and description.[/]"""
    library = _library()
    prompts = [
        prompt
        for prompt in library.all()
        if (kind is None or prompt.kind_name == kind)
        and (tag is None or tag.removeprefix("#") in prompt.tags)
        and matches(prompt, " ".join(query or []))
    ]
    if not out.is_terminal:
        for prompt in prompts:
            print(prompt.name, prompt.kind_name, prompt.description, sep="\t")
    elif prompts:
        render.listing(out, prompts)
    elif query or kind or tag:
        err.print(f"[{render.FAINT}]No prompts match.[/]")
    else:
        err.print(
            f"[{render.FAINT}]No prompts yet in {library.root}.[/]\n"
            "Create one with [bold]pil new NAME[/], or get a starter set with "
            "[bold]pil examples[/]."
        )


@app.command()
def show(
    name: Annotated[str, typer.Argument(autocompletion=_complete_names, show_default=False)],
) -> None:
    """Show a prompt: what it wants and what it says. [dim]Piped, it prints the file.[/]"""
    prompt = _find(name)
    if out.is_terminal:
        render.details(out, prompt, body=True)
    elif prompt.path is not None:
        sys.stdout.write(prompt.path.read_text(encoding="utf-8", errors="replace"))


STARTERS = {
    "session": "Work on {{task}}.",
    "transform": "Rewrite this text so that it's clearer. Keep its meaning and formatting.",
    "ask": "Explain what this is, briefly.",
    "copy": "{{text}}",
}


def _editor() -> list[str]:
    return shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")


def open_in_editor(path: Path) -> None:
    command = [*_editor(), str(path)]
    try:
        subprocess.run(command, check=False)
    except OSError as e:
        _fail(f"Couldn't start your editor ({command[0]}): {e.strerror or e}")


def _report(prompt: Prompt) -> None:
    """Say whether a prompt that was just written by hand is in working order."""
    if prompt.path is None:
        return
    fresh = parse(prompt.path.read_text(encoding="utf-8", errors="replace"), prompt.name)
    if fresh.error:
        _fail(
            f"{prompt.name} was saved, but it has a problem.",
            detail=fresh.error,
            hint=f"Fix it with [bold]pil edit {prompt.name}[/].",
        )


@app.command()
def new(
    name: Annotated[
        str, typer.Argument(help="A name, with folders if you like: [bold]work/ticket[/].")
    ],
    kind: Annotated[
        str, typer.Option("--kind", "-k", help=f"What it does: {', '.join(KINDS)}.")
    ] = DEFAULT_KIND,
    description: Annotated[
        str, typer.Option("--description", "-d", help="A line about what it's for.")
    ] = "",
    edit: Annotated[
        bool, typer.Option("--edit/--no-edit", help="Open it in your editor afterwards.")
    ] = True,
) -> None:
    """Create a prompt. [dim]Pipe text in to use it as the prompt's body.[/]"""
    if kind not in KINDS:
        _fail(f"There's no kind called {kind!r}.", hint=f"Choose from: {', '.join(KINDS)}.", code=2)
    piped = _read(sys.stdin).strip() if _stdin_is_piped(sys.stdin) else ""
    prompt = Prompt(
        name=name.removesuffix(".md"),
        kind_name=kind,
        description=description,
        body=piped or STARTERS[kind],
    )
    try:
        _library().save(prompt)
    except PromptError as e:
        _fail(str(e))
    assert prompt.path is not None
    if edit and not piped and sys.stdin.isatty() and out.is_terminal:
        open_in_editor(prompt.path)
        _report(prompt)
    render.done(err, f"Created {prompt.name}", str(prompt.path))


@app.command()
def edit(
    name: Annotated[str, typer.Argument(autocompletion=_complete_names, show_default=False)],
) -> None:
    """Open a prompt in your editor ($VISUAL or $EDITOR)."""
    prompt = _find(name)
    assert prompt.path is not None
    open_in_editor(prompt.path)
    _report(prompt)


@app.command()
def mv(
    name: Annotated[str, typer.Argument(autocompletion=_complete_names, show_default=False)],
    new_name: Annotated[str, typer.Argument(help="Its new name, folders included.")],
) -> None:
    """Rename a prompt, or move it to another folder."""
    prompt = _find(name)
    old = prompt.name
    try:
        _library().move(prompt, new_name.removesuffix(".md"))
    except (PromptError, OSError) as e:
        _fail(str(e))
    render.done(err, f"Moved {old} to {prompt.name}")


@app.command()
def rm(
    names: Annotated[list[str], typer.Argument(autocompletion=_complete_names, show_default=False)],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")] = False,
) -> None:
    """Delete prompts."""
    library = _library()
    prompts = [_find(name) for name in names]
    for prompt in prompts:
        if not yes and not typer.confirm(f"Delete {prompt.name}?", err=True):
            continue
        library.delete(prompt)
        render.done(err, f"Deleted {prompt.name}")


@app.command("dir")
def directory() -> None:
    """Print where your prompts live. [dim]Handy for `cd \"$(pil dir)\"` and for git.[/]"""
    print(config.prompts_dir())


@app.command()
def examples() -> None:
    """Add a starter set of prompts to your library, skipping any names already taken."""
    library = _library()
    for name, text in EXAMPLES.items():
        path = library.path_for(name)
        if path.exists():
            err.print(f"[{render.FAINT}]  {name} is already there[/]")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        added = parse(text, name)
        err.print(render.badge(added).append(f"  {added.description}", style=render.FAINT))
    render.done(err, "Starter prompts are in", str(library.root))


# ── The app itself ────────────────────────────────────────────────────────────


def _version(value: bool) -> None:
    if value:
        out.print(f"pil {__version__}")
        raise typer.Exit()


EPILOG = (
    "[bold]Examples[/]\n\n"
    "  [cyan]pil[/]                      browse, edit and run prompts in the TUI\n"
    "  [cyan]pil ticket ABC-123[/]       run a prompt by name\n"
    "  [cyan]pil ticket -h[/]            see what a prompt wants\n"
    "  [cyan]pil ls[/]                   list everything\n\n"
    "Prompts live in [bold]~/.config/pil/prompts[/] (see [bold]pil dir[/])."
)


@app.callback(epilog=EPILOG)
def root(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", "-V", callback=_version, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """The prompt invocation library for [bold]Claude Code[/]: fill prompts in and run them.

    With no arguments, [bold]pil[/] opens the TUI. [bold]pil NAME[/] runs a prompt.
    """
    if ctx.invoked_subcommand is not None:
        return
    if not (sys.stdin.isatty() and out.is_terminal):
        out.print(ctx.get_help())
        return
    from .tui.app import PilApp

    launch = PilApp(_library(), _config()).run()
    if launch:
        _launch(launch)


def main() -> None:
    app()
