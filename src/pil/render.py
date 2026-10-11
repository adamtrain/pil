"""How pil's command-line output looks."""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import fill
from .store import BUILTINS, Prompt, Variable
from .template import TAG

ACCENT = "#7c83f7"
ERROR = "#f2506e"
OK = "#1fbf8f"
FAINT = "grey42"
PLACEHOLDER = f"bold {ACCENT}"
SECTION = "bold #a871f7"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def error(console: Console, message: str, *, detail: str | None = None, hint: str | None = None):
    console.print(Text.assemble(("✗ ", f"bold {ERROR}"), (message, "bold")))
    if detail:
        console.print(Padding(Text(detail, style=FAINT), (0, 0, 0, 2)))
    if hint:
        console.print(Padding(Text.from_markup(hint), (0, 0, 0, 2)))


def done(console: Console, message: str, *facts: str) -> None:
    line = Text.assemble(("✓ ", f"bold {OK}"), (message, "bold"))
    for fact in facts:
        line.append(f" · {fact}", style=FAINT)
    console.print(line)


def source_line(origin: str, text: str, width: int) -> Text:
    """A one-line preview of what's about to be sent, so there are no surprises."""
    line = Text()
    line.append("◇ ", style=ACCENT)
    line.append(origin, style=f"bold {ACCENT}")
    line.append(f" · {plural(len(text.split()), 'word')} · ", style=FAINT)
    line.append(f"“{' '.join(text.split())}”", style=f"italic {FAINT}")
    line.truncate(width, overflow="ellipsis")
    return line


def working(prompt: Prompt) -> Text:
    return Text.assemble(("Asking Claude", ACCENT), (f" · {prompt.name}", FAINT))


def badge(prompt: Prompt) -> Text:
    """A prompt's kind glyph and name, with the folder part dimmed."""
    text = Text()
    if prompt.error:
        text.append("! ", style=f"bold {ERROR}")
    else:
        text.append(f"{prompt.kind.glyph} ", style=prompt.kind.color)
    if prompt.folder:
        text.append(f"{prompt.folder}/", style=FAINT)
    text.append(prompt.title, style="bold")
    return text


def highlight(body: str) -> Text:
    """A prompt's text with its placeholders picked out."""
    text = Text(body)
    for match in TAG.finditer(body):
        escaped, sigil, _ = match.groups()
        if not escaped:
            text.stylize(SECTION if sigil else PLACEHOLDER, *match.span())
    return text


def _synopsis(prompt: Prompt) -> str:
    try:
        return fill.synopsis(prompt.variables())
    except Exception:
        return ""


def listing(console: Console, prompts: Sequence[Prompt]) -> None:
    table = Table(box=None, show_header=False, padding=(0, 3, 0, 0), pad_edge=False, expand=True)
    table.add_column(no_wrap=True, min_width=max((len(p.name) for p in prompts), default=0) + 2)
    table.add_column(style=ACCENT, no_wrap=True, overflow="ellipsis", max_width=36)
    table.add_column(style=FAINT, no_wrap=True, overflow="ellipsis", ratio=1)
    for prompt in prompts:
        note = Text(prompt.error, style=ERROR) if prompt.error else Text(prompt.description)
        table.add_row(badge(prompt), Text(_synopsis(prompt)), note)
    console.print(table)


def _default(variable: Variable) -> str:
    if variable.required:
        return "required"
    if variable.type == "toggle" and variable.texts:
        return f"default: {'yes' if variable.default else 'no'}"
    if variable.type == "toggle":
        return "on" if variable.default else "off"
    return f"default: {variable.default}" if variable.default else "optional"


def variable_table(variables: Sequence[Variable]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 3, 0, 0), pad_edge=False)
    table.add_column(style=PLACEHOLDER, no_wrap=True)
    table.add_column(style=FAINT, no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(style=FAINT)
    for variable in variables:
        kind = variable.type
        if variable.options:
            kind = " | ".join(variable.options)
        elif variable.texts:
            kind = "yes | no"
        # What each answer puts in the prompt, one to a line beneath the description.
        about = [variable.help] if variable.help else []
        about += [f"{answer} → {text}" for answer, text in variable.texts if text]
        table.add_row(variable.name, kind, _default(variable), "\n".join(about))
    return table


def details(console: Console, prompt: Prompt, *, body: bool) -> None:
    console.print(summary(prompt, body=body))


def summary(prompt: Prompt, *, body: bool, command: str = "pil") -> Group:
    """Everything about one prompt: what it is, how to call it and, optionally, its text."""
    parts: list[RenderableType] = [
        Text.assemble(badge(prompt), (f" · {prompt.kind.name}", prompt.kind.color))
    ]
    if prompt.description:
        parts.append(Text(prompt.description))
    if prompt.tags:
        parts.append(Text(" ".join(f"#{tag}" for tag in prompt.tags), style=FAINT))
    if prompt.error:
        parts.append(Text.assemble(("✗ ", f"bold {ERROR}"), (prompt.error, ERROR)))
        if body:
            parts += [Rule(style=FAINT), Text(prompt.body)]
        return Group(*parts)

    variables = prompt.variables()
    usage = Text.assemble((f"{command} {prompt.name}", "bold"))
    if variables:
        usage.append(f" {fill.synopsis(variables)}", style=ACCENT)
    parts += [Text(), Padding(usage, (0, 0, 0, 2))]
    if variables:
        parts.append(Padding(variable_table(variables), (0, 0, 0, 2)))
    else:
        parts.append(Text())
    parts.append(Text(f"{prompt.kind.summary}.", style=FAINT))
    if prompt.options:
        settings = "  ".join(
            f"{key}={value}" for key, value in prompt.options.items() if key != "system"
        )
        if "system" in prompt.options:
            settings = f"{settings}  system=…".strip()
        parts.append(Text(f"Runs with {settings}.", style=FAINT))
    used = [name for name in BUILTINS if f"{{{{{name}}}}}" in prompt.body.replace(" ", "")]
    for name in used:
        parts.append(Text(f"{{{{{name}}}}} is {BUILTINS[name]}.", style=FAINT))
    if body:
        parts += [Rule(style=FAINT), highlight(prompt.body)]
    return Group(*parts)
