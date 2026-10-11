"""Small modal dialogs: a yes/no question and a one-line text box."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class Confirm(ModalScreen[bool]):
    """Ask a yes/no question. Dismisses with True for yes."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str, *, yes: str = "Yes", no: str = "No", danger: bool = False):
        super().__init__()
        self.question = question
        self.yes = yes
        self.no = no
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question, classes="dialog-title")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.no, id="no", compact=True)
                yield Button(
                    self.yes, id="yes", variant="error" if self.danger else "primary", compact=True
                )

    def on_mount(self) -> None:
        self.query_one("#no" if self.danger else "#yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class AskName(ModalScreen[str | None]):
    """Ask for a line of text. Dismisses with it, or None if cancelled."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, value: str = "", *, hint: str = ""):
        super().__init__()
        self.title_text = title
        self.value = value
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Input(self.value, id="answer")
            if self.hint:
                yield Label(self.hint, classes="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        answer = event.value.strip()
        self.dismiss(answer or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
