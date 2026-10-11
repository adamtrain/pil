"""The TUI: browse the library on the left, read a prompt on the right, and act on it."""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
from collections.abc import Callable
from typing import ClassVar

from rich.console import Group
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import render, runner, shorten
from ..clipboard import ClipboardError, read_clipboard, write_clipboard
from ..config import Config
from ..kinds import Runner
from ..store import Library, Prompt, PromptError, matches
from ..template import Value
from .dialogs import AskName, Confirm
from .editor import Editor
from .run import FillForm, ResultScreen
from .shorten import Outcome, ShortenScreen


class PromptList(OptionList):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "select", "Run")]


class Browser(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "filter", "Filter", key_display="/"),
        Binding("e", "edit", "Edit"),
        Binding("s", "shorten", "Shorten"),
        Binding("n", "new", "New"),
        Binding("c", "duplicate", "Duplicate"),
        Binding("m", "move", "Rename"),
        Binding("d", "delete", "Delete"),
        Binding("y", "copy", "Copy text"),
        Binding("E", "external", "$EDITOR"),
        Binding("r", "reload", "Reload", show=False),
        Binding("escape", "clear", "Clear filter", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, library: Library, config: Config):
        super().__init__()
        self.library = library
        self.config = config
        self.everything: list[Prompt] = []
        self.prompts: dict[str, Prompt] = {}

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter by name, #tag or kind", id="filter")
        with Horizontal(id="browser"):
            yield PromptList(id="prompts")
            with VerticalScroll(id="preview"):
                yield Static(id="details")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompts").border_title = "Prompts"
        self.reload()
        self.query_one("#prompts").focus()

    # ── The list ──────────────────────────────────────────────────────────────

    @property
    def selected(self) -> Prompt | None:
        option = self.query_one("#prompts", PromptList).highlighted_option
        return self.prompts.get(option.id) if option and option.id else None

    def reload(self, select: str | None = None) -> None:
        """Read the library again and redraw the list."""
        self.everything = self.library.all()
        self.redraw(select)

    def redraw(self, select: str | None = None) -> None:
        """Show the prompts that match the filter, keeping the selection if possible."""
        listing = self.query_one("#prompts", PromptList)
        current = select or (self.selected.name if self.selected else None)
        query = self.query_one("#filter", Input).value
        everything = self.everything
        shown = [prompt for prompt in everything if matches(prompt, query)]
        self.prompts = {prompt.name: prompt for prompt in shown}

        options: list[Option] = []
        folder = ""
        for prompt in shown:
            if prompt.folder != folder:
                folder = prompt.folder
                options.append(Option(Text(f"{folder}/", style="bold"), disabled=True))
            line = Text("  " if prompt.folder else "")
            if prompt.error:
                line.append("! ", style=f"bold {render.ERROR}")
            else:
                line.append(f"{prompt.kind.glyph} ", style=prompt.kind.color)
            line.append(prompt.title)
            options.append(Option(line, id=prompt.name))
        listing.set_options(options)
        listing.border_subtitle = (
            f"{len(shown)} of {len(everything)}" if query else render.plural(len(shown), "prompt")
        )

        if shown:
            # Headings have no id, so this only ever lands on a prompt.
            names = [option.id for option in options]
            listing.highlighted = names.index(current if current in self.prompts else shown[0].name)
        else:
            self._show(None, empty=not everything)

    def _show(self, prompt: Prompt | None, *, empty: bool = False) -> None:
        details = self.query_one("#details", Static)
        if prompt is not None:
            details.update(render.summary(prompt, body=True))
        elif empty:
            details.update(
                Group(
                    Text("No prompts yet.", style="bold"),
                    Text(),
                    Text.from_markup(
                        "Press [bold]n[/] to write your first one, or quit and run "
                        "[bold]pil examples[/] for a starter set."
                    ),
                    Text(),
                    Text(f"They'll live in {self.library.root}", style=render.FAINT),
                )
            )
        else:
            details.update(Text("Nothing matches that filter.", style=render.FAINT))

    @on(OptionList.OptionHighlighted, "#prompts")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._show(self.prompts.get(event.option_id or ""))
        self.query_one("#preview", VerticalScroll).scroll_home(animate=False)

    # ── Filtering ─────────────────────────────────────────────────────────────

    def action_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_clear(self) -> None:
        self.query_one("#filter", Input).value = ""
        self.query_one("#prompts").focus()

    @on(Input.Changed, "#filter")
    def _filtered(self) -> None:
        self.redraw()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        self.query_one("#prompts").focus()
        self.action_run()

    def on_key(self, event: events.Key) -> None:
        # Arrow keys in the filter box move through the list, so you never have to leave it.
        if self.query_one("#filter").has_focus and event.key in ("up", "down"):
            listing = self.query_one("#prompts", PromptList)
            listing.action_cursor_down() if event.key == "down" else listing.action_cursor_up()
            event.stop()

    # ── Running ───────────────────────────────────────────────────────────────

    @on(OptionList.OptionSelected, "#prompts")
    def _chosen(self) -> None:
        self.action_run()

    def action_run(self) -> None:
        prompt = self.selected
        if prompt is None:
            return
        if prompt.error:
            self.notify(prompt.error, title=f"{prompt.name} needs fixing", severity="error")
            return
        if not prompt.variables():
            self._run(prompt, {})
            return

        def filled(values: dict[str, Value] | None) -> None:
            if values is not None:
                self._run(prompt, values)

        self.app.push_screen(FillForm(prompt), filled)

    def _run(self, prompt: Prompt, values: dict[str, Value]) -> None:
        content = None
        try:
            if prompt.takes_input:
                content = read_clipboard()
                if not content.strip():
                    self.notify(
                        "Copy some text first.", title="The clipboard is empty", severity="warning"
                    )
                    return
            invocation = runner.prepare(prompt, values, self.config, input_text=content)
            if invocation.runner is Runner.SESSION:
                self.app.exit(invocation.argv)
            elif invocation.runner is Runner.NONE:
                write_clipboard(invocation.text)
                words = render.plural(len(invocation.text.split()), "word")
                self.notify(f"{words} copied to the clipboard.", title=prompt.name)
            else:
                self.app.push_screen(ResultScreen(invocation))
        except (runner.RunError, ClipboardError) as e:
            self.notify(str(e), severity="error")

    # ── Organizing ────────────────────────────────────────────────────────────

    def _edited(self, saved: Prompt | None) -> None:
        self.reload(select=saved.name if saved else None)

    def action_edit(self) -> None:
        if self.selected is not None:
            self.app.push_screen(Editor(self.library, self.selected), self._edited)

    def action_shorten(self) -> None:
        """Have Claude shorten the prompt's text as far as it will go, to accept or not."""
        prompt = self.selected
        if prompt is None:
            return
        if prompt.error or not prompt.body.strip():
            reason = prompt.error or "It has no text yet."
            self.notify(reason, title=f"{prompt.name} can't be shortened", severity="error")
            return

        if shorten.is_spent(prompt.body):
            # Already taken as far as it goes, and not edited since.
            self.notify("It can't get any shorter.", title=prompt.name)
            return

        def decided(outcome: Outcome | None) -> None:
            if outcome is None:
                return
            text = prompt.body
            if outcome.text is None:
                self.notify("It can't get any shorter.", title=prompt.name)
            else:
                try:
                    self.library.replace_body(prompt, outcome.text)
                except (PromptError, OSError) as e:
                    self.notify(str(e), title="Not replaced", severity="error")
                    return
                self.reload(select=prompt.name)
                saved = render.plural(len(text) - len(outcome.text), "character")
                self.notify(f"{saved} shorter.", title=prompt.name)
                text = outcome.text
            if outcome.spent:
                # Losing this only costs a wasted run next time, so don't make a fuss.
                with contextlib.suppress(OSError):
                    shorten.mark_spent(text, self.library)

        self.app.push_screen(ShortenScreen(prompt, self.config), decided)

    def action_new(self) -> None:
        folder = self.selected.folder if self.selected else ""
        # Start in the folder being looked at, since that's usually where a new prompt belongs.
        draft = Prompt(name=f"{folder}/" if folder else "")
        self.app.push_screen(Editor(self.library, draft), self._edited)

    def _rename(self, title: str, start: str, apply: Callable[[Prompt, str], Prompt]) -> None:
        prompt = self.selected
        if prompt is None:
            return

        def named(name: str | None) -> None:
            if not name or name == prompt.name:
                return
            try:
                result = apply(prompt, name.removesuffix(".md"))
            except (PromptError, OSError) as e:
                self.notify(str(e), severity="error")
            else:
                self.reload(select=result.name)

        hint = "Use / to put it in a folder."
        self.app.push_screen(AskName(title, start or prompt.name, hint=hint), named)

    def action_move(self) -> None:
        self._rename("Rename or move", "", self.library.move)

    def action_duplicate(self) -> None:
        if self.selected is not None:
            self._rename("Duplicate as", f"{self.selected.name}-copy", self.library.copy)

    def action_delete(self) -> None:
        prompt = self.selected
        if prompt is None:
            return

        def decided(delete: bool | None) -> None:
            if delete:
                self.library.delete(prompt)
                self.reload()

        self.app.push_screen(Confirm(f"Delete {prompt.name}?", yes="Delete", danger=True), decided)

    def action_copy(self) -> None:
        if self.selected is None:
            return
        try:
            write_clipboard(self.selected.body)
        except ClipboardError as e:
            self.notify(str(e), severity="error")
        else:
            self.notify("Its text is on the clipboard, placeholders and all.", title="Copied")

    def action_external(self) -> None:
        prompt = self.selected
        if prompt is None or prompt.path is None:
            return
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        try:
            with self.app.suspend():
                subprocess.run([*editor, str(prompt.path)], check=False)
        except OSError as e:
            self.notify(f"Couldn't start {editor[0]}: {e.strerror or e}", severity="error")
        self.reload()

    def action_reload(self) -> None:
        self.reload()

    def action_quit(self) -> None:
        self.app.exit(None)


class PilApp(App[list[str] | None]):
    """Returns the command for a Claude Code session to start once the TUI has closed."""

    TITLE = "pil"
    CSS_PATH = "pil.tcss"

    def __init__(self, library: Library, config: Config):
        super().__init__()
        self.library = library
        self.config = config

    def get_default_screen(self) -> Screen:
        return Browser(self.library, self.config)
