"""Running a prompt from the TUI: a form for its variables, then a screen for the result."""

from __future__ import annotations

import time
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Input,
    Label,
    LoadingIndicator,
    Markdown,
    Select,
    Static,
)

from .. import render, runner
from ..clipboard import ClipboardError, write_clipboard
from ..kinds import Output
from ..store import Prompt, Variable
from ..template import TemplateError, Value


class FillForm(ModalScreen[dict[str, Value] | None]):
    """Collect values for a prompt's variables. Dismisses with them, or None if cancelled."""

    AUTO_FOCUS = "#fields Input, #fields Checkbox, #fields Select, #run"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+r", "submit", "Run"),
    ]

    def __init__(self, prompt: Prompt):
        super().__init__()
        self.prompt = prompt
        self.variables = prompt.variables()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(render.badge(self.prompt), classes="dialog-title")
            if self.prompt.description:
                yield Label(self.prompt.description, classes="hint")
            with VerticalScroll(id="fields", can_focus=False):
                for variable in self.variables:
                    with Horizontal(classes="field"):
                        yield Label(variable.name, classes="field-name")
                        yield self._widget(variable)
            with VerticalScroll(id="filled-box", can_focus=False) as preview:
                preview.border_title = "Filled in"
                yield Static(id="filled")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel", compact=True)
                yield Button(
                    f"{self.prompt.kind.glyph} Run", id="run", variant="primary", compact=True
                )

    def _widget(self, variable: Variable):
        field_id = f"var-{variable.name}"
        if variable.type == "toggle" and variable.required:
            # A question with no default has to be answered, which a checkbox can't insist on.
            answers = [("Yes", True), ("No", False)]
            return Select(answers, prompt=variable.help or "Yes or no?", compact=True, id=field_id)
        if variable.type == "toggle":
            return Checkbox(variable.help, bool(variable.default), compact=True, id=field_id)
        if variable.type == "choice":
            options = [(option, option) for option in variable.options]
            if variable.default is None:
                return Select(options, prompt=variable.help or "Choose…", compact=True, id=field_id)
            return Select(
                options, value=variable.default, allow_blank=False, compact=True, id=field_id
            )
        placeholder = variable.help or ("" if variable.required else "optional")
        return Input(
            str(variable.default or ""), placeholder=placeholder, compact=True, id=field_id
        )

    def on_mount(self) -> None:
        self._refresh_preview()

    def _read(self, variable: Variable) -> Value | None:
        """A variable's value as the form has it, or None if it's been left blank."""
        widget = self.query_one(f"#var-{variable.name}")
        if isinstance(widget, Checkbox):
            return widget.value
        if isinstance(widget, Select):
            if widget.value is Select.NULL:
                return None
            return widget.value if isinstance(widget.value, bool) else str(widget.value)
        assert isinstance(widget, Input)
        if widget.value.strip() or not variable.required:
            return widget.value
        return None

    def _refresh_preview(self) -> None:
        # Anything not filled in yet stays as its placeholder.
        values: dict[str, Value] = {"input": "{{input}}", "clipboard": "{{clipboard}}"}
        for variable in self.variables:
            value = self._read(variable)
            values[variable.name] = f"{{{{{variable.name}}}}}" if value is None else value
        try:
            text = Text(runner.fill(self.prompt, values))
        except (TemplateError, ClipboardError) as e:
            text = Text(str(e), style=render.ERROR)
        text.highlight_regex(r"\{\{[\w-]+\}\}", render.PLACEHOLDER)
        if self.prompt.kind.takes_input:
            text.append("\n\n+ the clipboard's text", style=render.FAINT)
        self.query_one("#filled", Static).update(text)

    def on_input_changed(self) -> None:
        self._refresh_preview()

    def on_checkbox_changed(self) -> None:
        self._refresh_preview()

    def on_select_changed(self) -> None:
        self._refresh_preview()

    def on_input_submitted(self) -> None:
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self.action_submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        values: dict[str, Value] = {}
        for variable in self.variables:
            value = self._read(variable)
            if value is None:
                # Something required is still blank: go there rather than complain.
                self.query_one(f"#var-{variable.name}").focus()
                return
            values[variable.name] = value
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class JobScreen[T](Screen[T | None]):
    """Runs one `claude -p` job, with a clock while it works. Subclasses say what to do with
    the result."""

    waiting = "asking Claude"

    def __init__(self, invocation: runner.Invocation, subject: Prompt | None = None):
        super().__init__()
        self.invocation = invocation
        self.subject = subject or invocation.prompt
        self.job = runner.Job(invocation)
        self.started = time.monotonic()
        self.closed = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with VerticalScroll(id="result"):
            yield LoadingIndicator(id="waiting")
        yield Footer()

    def on_mount(self) -> None:
        self._tick()
        self.ticker = self.set_interval(0.1, self._tick)
        self._run()

    def _status(self, *facts: str, mark: str = "", style: str = render.ACCENT) -> None:
        line = Text.assemble((mark, f"bold {style}"), render.badge(self.subject))
        for fact in facts:
            line.append(f" · {fact}", style=render.FAINT)
        self.query_one("#status", Static).update(line)

    def _tick(self) -> None:
        self._status(self.waiting, f"{time.monotonic() - self.started:.1f}s")

    @work(thread=True)
    def _run(self) -> None:
        try:
            result = self.job.run()
        except runner.Cancelled:
            return
        except runner.RunError as e:
            self.app.call_from_thread(self._failed, e)
        else:
            self.app.call_from_thread(self._finished, result)

    async def _show(self, *widgets: Widget) -> None:
        self.ticker.stop()
        box = self.query_one("#result", VerticalScroll)
        await box.remove_children()
        await box.mount(*widgets)
        box.focus()

    async def _failed(self, error: runner.RunError) -> None:
        if self.closed:
            return
        self._status("failed", mark="✗ ", style=render.ERROR)
        message = Text(str(error), style="bold")
        if error.detail:
            message.append(f"\n\n{error.detail}", style=render.FAINT)
        if error.hint:
            message.append("\n\n").append_text(Text.from_markup(error.hint))
        await self._show(Static(message))

    async def _finished(self, result: runner.Result) -> None:
        # Backed out of just as the answer arrived: act as if it never did.
        if not self.closed:
            await self.finished(result)
            self.refresh_bindings()

    async def finished(self, result: runner.Result) -> None:
        raise NotImplementedError

    def action_close(self) -> None:
        self.closed = True
        self.job.cancel()
        self.dismiss(None)


class ResultScreen(JobScreen[None]):
    """Run a one-shot prompt and show what comes back."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "close", "Back"),
        Binding("y", "copy", "Copy"),
        Binding("u", "undo", "Restore clipboard"),
    ]

    def __init__(self, invocation: runner.Invocation):
        super().__init__(invocation)
        self.text: str | None = None
        self.replaced_clipboard = False

    async def finished(self, result: runner.Result) -> None:
        invocation = self.invocation
        self.text = runner.settle(invocation, result.text)
        facts = [f"{result.seconds:.1f}s"]
        if result.model:
            facts.append(result.model)
        if invocation.output is Output.CLIPBOARD:
            try:
                write_clipboard(self.text)
            except ClipboardError as e:
                self.notify(str(e), severity="error")
            else:
                self.replaced_clipboard = True
                before = len((invocation.input_text or "").split())
                after = render.plural(len(self.text.split()), "word")
                facts = ["clipboard updated", f"{before:,} → {after}", *facts]
            await self._show(Static(Text(self.text)))
        else:
            await self._show(Markdown(self.text))
        self._status(*facts, mark="✓ ", style=render.OK)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "copy":
            return self.text is not None
        if action == "undo":
            return self.replaced_clipboard
        return True

    def _copy(self, text: str, message: str) -> None:
        try:
            write_clipboard(text)
        except ClipboardError as e:
            self.notify(str(e), severity="error")
        else:
            self.notify(message)

    def action_copy(self) -> None:
        if self.text is not None:
            self._copy(self.text, "Copied the result to the clipboard.")
            self.replaced_clipboard = self.invocation.input_text is not None
            self.refresh_bindings()

    def action_undo(self) -> None:
        if self.invocation.input_text is not None:
            self._copy(self.invocation.input_text, "Put the original text back on the clipboard.")
            self.replaced_clipboard = False
            self.refresh_bindings()
