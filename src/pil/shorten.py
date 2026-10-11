"""Ask Claude for a shorter version of a prompt's text, again and again until it stops paying.

Each pass starts from the last one's result. A pass counts only if it's enough shorter, still a
usable template, and has the same variables as the original. Once a text can't be shortened any
further, that's remembered, so asking again costs nothing. The memory is tied to the text
itself: change the prompt by any means and it no longer applies.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from . import config, runner, template
from .config import Config
from .kinds import TRANSFORM_SYSTEM
from .store import Library, Prompt

INSTRUCTION = "Maximally shorten this text, keeping information, grammar, and LLM-prompt usability."

# The text is a template, and a tag that comes back reworded is a variable lost.
SYSTEM = (
    f"{TRANSFORM_SYSTEM} The text is a prompt template: copy every {{{{…}}}} tag in it "
    "exactly as written."
)

# Each pass has to cut 5%, so this is far more than a real prompt needs. It's a backstop.
MAX_PASSES = 10


def invocation(prompt: Prompt, cfg: Config, text: str | None = None) -> runner.Invocation:
    """A one-shot run that shortens a prompt's text, set up like any other transform."""
    task = Prompt(
        name=prompt.name, kind_name="transform", body=INSTRUCTION, options={"system": SYSTEM}
    )
    return runner.prepare(task, {}, cfg, input_text=prompt.body if text is None else text)


def worthwhile(before: str, after: str) -> bool:
    """True when the shorter text saves at least 5% or 5 characters, whichever is more."""
    return len(before) - len(after) >= max(len(before) * 0.05, 5)


def tag_changes(before: str, after: str) -> tuple[list[str], list[str]]:
    """The variables a rewrite dropped and the ones it introduced.

    Raises TemplateError if the rewrite isn't a usable template at all.
    """
    old, new = template.variables(before), template.variables(after)
    return [name for name in old if name not in new], [name for name in new if name not in old]


class Verdict(StrEnum):
    SHORTER = "shorter"  # worth keeping, and worth trying to shorten again
    SPENT = "spent"  # not enough shorter: the text before this pass is as short as it gets
    BROKEN = "broken"  # shorter, but no longer the same prompt


def judge(original: str, before: str, after: str) -> tuple[Verdict, str]:
    """Decide what one pass achieved, with the reason if it broke the prompt."""
    if not worthwhile(before, after):
        return Verdict.SPENT, ""
    try:
        dropped, added = tag_changes(original, after)
    except template.TemplateError as e:
        return Verdict.BROKEN, f"The shorter version isn't a usable prompt: {e}."
    changes = [
        f"{verb} {', '.join(f'{{{{{name}}}}}' for name in names)}"
        for verb, names in (("dropped", dropped), ("added", added))
        if names
    ]
    if changes:
        return Verdict.BROKEN, f"The shorter version {' and '.join(changes)}."
    return Verdict.SHORTER, ""


# ── Remembering what's already as short as it gets ────────────────────────────


def _state_path():
    return config.home() / "state.json"


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _spent() -> set[str]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return {str(digest) for digest in data["shortened"]}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def is_spent(text: str) -> bool:
    """True if shortening this exact text has already been tried and got nowhere."""
    return _digest(text) in _spent()


def mark_spent(text: str, library: Library) -> None:
    """Remember that a text can't be shortened further, forgetting texts no prompt has now."""
    current = {_digest(prompt.body) for prompt in library.all()}
    digests = (_spent() & current) | {_digest(text)}
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"shortened": sorted(digests)}, indent=2) + "\n", encoding="utf-8")
