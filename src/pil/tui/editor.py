"""The prompt editor: a form for the details, a text area for the prompt, and a card for each
variable the text uses."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Checkbox, Footer, Input, Label, Select, Static, TextArea

from .. import template
from ..kinds import DEFAULT_MODEL, KINDS
from ..store import BUILTINS, VAR_TYPES, Library, Prompt, PromptError, Variable
from .dialogs import Confirm


def _words(text: str) -> list[str]:
    return [word.strip().removeprefix("#") for word in text.replace(",", " ").split()]


class VariableCard(Vertical):
    """The settings for one variable. Which fields show depends on the variable's type."""

    def __init__(self, variable: Variable, use: str):
        super().__init__(classes=f"card type-{variable.type}")
        self.variable = variable
        self.use = use
        texts = dict(variable.texts)
        # Options are edited as one line, "bug=resolve, feature=implement", which can't hold
        # texts with commas in them. Those are shown but left exactly as the file has them.
        self.fixed_options = variable.type == "choice" and any(
            "," in part or "=" in answer
            for answer, text in variable.texts
            for part in (answer, text)
        )
        self.options_text = ", ".join(
            f"{option}={texts[option]}" if option in texts else option
            for option in variable.options
        )

    def compose(self) -> ComposeResult:
        variable = self.variable
        with Horizontal(classes="card-head"):
            yield Label(f"{{{{{variable.name}}}}}", classes="card-name")
            yield Select(
                [(kind, kind) for kind in VAR_TYPES],
                value=variable.type,
                allow_blank=False,
                compact=True,
                classes="card-type",
            )
        with Horizontal(classes="card-row"):
            yield Label("About", classes="card-label")
            yield Input(
                variable.help, placeholder="What it's for", compact=True, classes="card-help"
            )
        with Horizontal(classes="card-row for-choice"):
            yield Label("Options", classes="card-label")
            yield Input(
                self.options_text,
                placeholder="small, large  or  bug=resolve, feature=implement",
                compact=True,
                disabled=self.fixed_options,
                classes="card-options",
            )
        yes_no = dict(variable.texts) if variable.type == "toggle" else {}
        with Horizontal(classes="card-row for-toggle"):
            yield Label("If yes", classes="card-label")
            yield Input(
                yes_no.get("yes", ""), placeholder="Text to add", compact=True, classes="card-yes"
            )
        with Horizontal(classes="card-row for-toggle"):
            yield Label("If no", classes="card-label")
            yield Input(
                yes_no.get("no", ""), placeholder="Text to add", compact=True, classes="card-no"
            )
        unset = "no"
        if variable.type == "toggle":
            unset = "ask" if variable.default is None else "yes" if variable.default else "no"
        with Horizontal(classes="card-row for-toggle"):
            yield Label("Default", classes="card-label")
            yield Select(
                [("no", "no"), ("yes", "yes"), ("ask each time", "ask")],
                value=unset,
                allow_blank=False,
                compact=True,
                classes="card-unset",
            )
        default = "" if isinstance(variable.default, bool) else variable.default or ""
        with Horizontal(classes="card-row for-value"):
            yield Label("Default", classes="card-label")
            yield Input(default, placeholder="None", compact=True, classes="card-default")
        yield Checkbox(
            "Can be left empty",
            variable.type == "text" and variable.default == "",
            compact=True,
            classes="card-optional for-text",
        )

    @property
    def type(self) -> str:
        return str(self.query_one(".card-type", Select).value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.has_class("card-type"):
            for kind in VAR_TYPES:
                self.set_class(event.value == kind, f"type-{kind}")

    def _options(self) -> Any:
        """The options as the frontmatter should have them: a list, or a table of texts."""
        if self.fixed_options:
            return dict(self.variable.texts)
        pairs = [
            (answer.strip(), text.strip() if equals else None)
            for answer, equals, text in (
                part.partition("=")
                for part in self.query_one(".card-options", Input).value.split(",")
            )
            if answer.strip()
        ]
        if any(text is not None for _, text in pairs):
            return {answer: answer if text is None else text for answer, text in pairs}
        return [answer for answer, _ in pairs]

    def spec(self) -> Any:
        """This variable as it should be written in the prompt's frontmatter, or None if
        everything about it can be worked out from the prompt's text."""
        spec: dict[str, Any] = {}
        kind = self.type
        help_text = self.query_one(".card-help", Input).value.strip()
        default = self.query_one(".card-default", Input).value
        if kind == "toggle":
            yes = self.query_one(".card-yes", Input).value.strip()
            no = self.query_one(".card-no", Input).value.strip()
            unset = self.query_one(".card-unset", Select).value
            if yes or no or unset == "ask":
                # Either answer can put text in the prompt, and it's asked unless given a default.
                spec["options"] = {True: yes, False: no}
                if unset != "ask":
                    spec["default"] = unset == "yes"
            else:
                if self.use != "toggle":
                    spec["type"] = "toggle"
                if unset == "yes":
                    spec["default"] = True
        elif kind == "choice":
            spec["options"] = self._options()
            if default.strip():
                spec["default"] = default.strip()
        else:
            if self.use == "toggle":
                spec["type"] = "text"
            if default:
                spec["default"] = default
            elif self.query_one(".card-optional", Checkbox).value and self.use != "optional":
                spec["default"] = ""
        if help_text:
            spec["help"] = help_text
        if list(spec) == ["help"]:
            return help_text
        return spec or None


class Editor(Screen[Prompt | None]):
    """Edit a prompt, or write a new one. Dismisses with the saved prompt, or None."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, library: Library, prompt: Prompt):
        super().__init__()
        self.library = library
        self.prompt = prompt
        self.cards: dict[str, VariableCard] = {}
        self.initial = ""

    def compose(self) -> ComposeResult:
        prompt = self.prompt
        with Horizontal(id="editor"):
            with Vertical(id="form"):
                with Horizontal(classes="field"):
                    yield Label("Name", classes="field-name")
                    yield Input(
                        prompt.name,
                        placeholder="folder/name",
                        select_on_focus=False,
                        compact=True,
                        id="name",
                    )
                with Horizontal(classes="field"):
                    yield Label("Kind", classes="field-name")
                    yield Select(
                        [(f"{kind.glyph} {kind.name}", kind.name) for kind in KINDS.values()],
                        value=prompt.kind.name,
                        allow_blank=False,
                        compact=True,
                        id="kind",
                    )
                yield Label("", id="kind-summary", classes="hint")
                with Horizontal(classes="field"):
                    yield Label("About", classes="field-name")
                    yield Input(
                        prompt.description,
                        placeholder="A line about what it's for",
                        compact=True,
                        id="about",
                    )
                with Horizontal(classes="field"):
                    yield Label("Tags", classes="field-name")
                    yield Input(
                        " ".join(prompt.tags), placeholder="work review", compact=True, id="tags"
                    )
                with Horizontal(classes="field"):
                    yield Label("Model", classes="field-name")
                    yield Input(
                        str(prompt.options.get("model", "")),
                        placeholder=DEFAULT_MODEL,
                        compact=True,
                        id="model",
                    )
                body = TextArea(prompt.body, soft_wrap=True, id="body")
                body.border_title = "Prompt"
                yield body
            with VerticalScroll(id="variables") as variables:
                variables.border_title = "Variables"
                variables.can_focus = False
                yield Static(id="variables-note", classes="hint")
        yield Footer()

    async def on_mount(self) -> None:
        self._show_kind(self.prompt.kind.name)
        await self._sync_cards()
        self.initial = self._draft().to_text()
        self.query_one("#name" if self.prompt.path is None else "#body").focus()

    def _show_kind(self, name: str) -> None:
        self.query_one("#kind-summary", Label).update(f"{KINDS[name].summary}.")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "kind":
            self._show_kind(str(event.value))

    async def on_text_area_changed(self) -> None:
        await self._sync_cards()

    async def _sync_cards(self) -> None:
        """Keep one card per variable in the prompt's text, in the order they appear."""
        note = self.query_one("#variables-note", Static)
        body = self.query_one("#body", TextArea).text
        try:
            used = template.variables(body)
        except template.TemplateError as e:
            # Mid-edit, sections are often unbalanced. Leave the cards alone until they aren't.
            note.update(f"[b]The prompt's text has a problem on {e}.[/]")
            return
        used = {name: use for name, use in used.items() if name not in BUILTINS}

        for name in [name for name in self.cards if name not in used]:
            await self.cards.pop(name).remove()
        draft = replace(self.prompt, body=body)
        try:
            known = {variable.name: variable for variable in draft.variables()}
        except PromptError:
            known = {}
        panel = self.query_one("#variables", VerticalScroll)
        for name, use in used.items():
            card = self.cards.get(name)
            if card is None:
                card = VariableCard(known.get(name) or Variable(name), use)
                self.cards[name] = card
                await panel.mount(card, before=note)
            card.use = use
        if self.cards:
            note.update(
                "A card appears for every [b]{{name}}[/] in the prompt. "
                "[b]{{#name}}[/]…[b]{{/name}}[/] keeps text only when [i]name[/] is on or "
                "filled in."
            )
        else:
            note.update(
                "No variables yet. Write [b]{{name}}[/] in the prompt for a part that changes, "
                "or wrap optional text in [b]{{#name}}[/]…[b]{{/name}}[/]."
            )

    def _draft(self) -> Prompt:
        """The prompt as the form currently has it."""
        options = dict(self.prompt.options)
        model = self.query_one("#model", Input).value.strip()
        if model:
            options["model"] = model
        else:
            options.pop("model", None)
        specs = {name: card.spec() for name, card in self.cards.items()}
        return replace(
            self.prompt,
            name=self.query_one("#name", Input).value.strip().removesuffix(".md"),
            kind_name=str(self.query_one("#kind", Select).value),
            description=self.query_one("#about", Input).value.strip(),
            tags=_words(self.query_one("#tags", Input).value),
            options=options,
            vars={name: spec for name, spec in specs.items() if spec is not None},
            body=self.query_one("#body", TextArea).text.strip(),
        )

    def action_save(self) -> None:
        draft = self._draft()
        if not draft.name:
            self.notify("Give the prompt a name first.", severity="warning")
            self.query_one("#name", Input).focus()
            return
        try:
            self.library.save(draft)
        except (PromptError, OSError) as e:
            self.notify(str(e), title="Not saved", severity="error")
            return
        self.dismiss(draft)

    def action_cancel(self) -> None:
        if self._draft().to_text() == self.initial:
            self.dismiss(None)
            return

        def decided(discard: bool | None) -> None:
            if discard:
                self.dismiss(None)

        self.app.push_screen(
            Confirm("Discard your changes?", yes="Discard", no="Keep editing", danger=True),
            decided,
        )
