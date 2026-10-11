"""Shorten a prompt's text as far as it will go, then offer the result beside the original."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from .. import render, runner, shorten
from ..config import Config
from ..shorten import Verdict
from ..store import Prompt
from .run import JobScreen


@dataclass(frozen=True, slots=True)
class Outcome:
    """How shortening ended."""

    # The shorter text to save, if one was approved.
    text: str | None
    # Whether shortening ran its course, so what's left is as short as it gets.
    spent: bool


class ShortenScreen(JobScreen[Outcome]):
    """Runs pass after pass until one stops paying. Dismisses with an Outcome, or None if
    there was nothing to decide or the offer was turned down."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y,enter", "accept", "Replace"),
        Binding("n,escape,q", "close", "Keep the original"),
    ]

    waiting = "shortening"

    def __init__(self, prompt: Prompt, config: Config):
        super().__init__(shorten.invocation(prompt, config), subject=prompt)
        self.prompt = prompt
        self.config = config
        self.best = prompt.body
        self.passes = 0
        self.model: str | None = None
        self.proposal: str | None = None
        self.spent = False

    def _tick(self) -> None:
        facts = [self.waiting]
        if self.passes:
            facts += [f"pass {self.passes + 1}", self._sizes()]
        self._status(*facts, f"{time.monotonic() - self.started:.1f}s")

    def _sizes(self) -> str:
        return f"{len(self.prompt.body):,} → {render.plural(len(self.best), 'character')}"

    async def finished(self, result: runner.Result) -> None:
        after = result.text.strip()
        verdict, reason = shorten.judge(self.prompt.body, self.best, after)
        if verdict is not Verdict.SHORTER:
            await self._conclude(spent=verdict is Verdict.SPENT, note=reason)
            return
        self.best, self.passes, self.model = after, self.passes + 1, result.model
        if self.passes >= shorten.MAX_PASSES:
            await self._conclude(spent=False)
            return
        # Shorter, so see whether the shorter text will go shorter still.
        self.job = runner.Job(shorten.invocation(self.prompt, self.config, after))
        self._run()

    async def _failed(self, error: runner.RunError) -> None:
        if self.passes and self.proposal is None and not self.closed:
            # A later pass fell over, but the earlier ones are still worth offering.
            await self._conclude(spent=False, note=f"Stopped early: {error}")
        else:
            await super()._failed(error)

    async def _conclude(self, *, spent: bool, note: str = "") -> None:
        """Stop making passes and offer the last text that was worth having, if any was."""
        if not self.passes and spent:
            self.dismiss(Outcome(None, spent=True))
            return
        if not self.passes:
            problem = runner.RunError("Claude's shorter version can't be used.", detail=note)
            await super()._failed(problem)
            return

        before, after = self.prompt.body, self.best
        widgets: list[Widget] = []
        if note:
            widgets.append(Static(Text(note, style=render.FAINT), id="note"))
        panes = []
        for title, text in (("Now", before), ("Shortened", after)):
            pane = Static(render.highlight(text), classes="pane")
            pane.border_title = f"{title} · {render.plural(len(text), 'character')}"
            panes.append(pane)
        await self._show(*widgets, Horizontal(*panes, id="compare"))

        self.proposal, self.spent = after, spent
        facts = [
            f"{1 - len(after) / len(before):.0%} shorter",
            f"{self.passes} pass{'' if self.passes == 1 else 'es'}",
            f"{time.monotonic() - self.started:.1f}s",
        ]
        if self.model:
            facts.append(self.model)
        self._status(*facts, mark="✓ ", style=render.OK)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return self.proposal is not None if action == "accept" else True

    def action_accept(self) -> None:
        self.dismiss(Outcome(self.proposal, spent=self.spent))
