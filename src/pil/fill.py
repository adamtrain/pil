"""Turn command-line arguments into values for a prompt's variables.

Each prompt gets a small command line of its own, built from its variables:

    pil ticket ABC-123 --no-tests --scope=large
    pil ticket ticket=ABC-123 scope=large

Bare words fill the text variables in order (required ones first), and whatever is left over
joins the last of them, so free text doesn't need quoting. Toggles and choices are flags.
"""

from __future__ import annotations

from collections.abc import Sequence

from .store import Variable
from .template import Value

TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}


class FillError(Exception):
    pass


def positional(variables: Sequence[Variable]) -> list[Variable]:
    """The variables bare words fill, in the order they fill them."""
    slots = [variable for variable in variables if variable.type == "text"]
    return sorted(slots, key=lambda variable: not variable.required)


def synopsis(variables: Sequence[Variable]) -> str:
    flags = [variable for variable in variables if variable.type != "text"]
    return " ".join(variable.usage for variable in [*positional(variables), *flags])


def coerce(variable: Variable, raw: str) -> Value:
    """Check a value typed for a variable and put it in the variable's own terms."""
    folded = raw.strip().lower()
    # The text an answer stands for will do in place of the answer.
    meant = [answer for answer, text in variable.texts if text and text.lower() == folded]
    if variable.type == "toggle":
        if folded in TRUTHY or meant == ["yes"]:
            return True
        if folded in FALSY or meant == ["no"]:
            return False
        raise FillError(f"{variable.name} is a yes or no: give it yes or no, not {raw!r}.")
    if variable.type == "choice":
        exact = [option for option in variable.options if option.lower() == folded] or meant
        starts = [option for option in variable.options if option.lower().startswith(folded)]
        if exact or (folded and len(starts) == 1):
            return (exact or starts)[0]
        raise FillError(
            f"{variable.name} should be one of {', '.join(variable.options)}, not {raw!r}."
        )
    return raw


def parse_args(variables: Sequence[Variable], args: Sequence[str]) -> dict[str, Value]:
    """Read values out of a prompt's arguments. Variables not mentioned are left out."""
    by_name = {variable.name: variable for variable in variables}
    values: dict[str, Value] = {}
    words: list[str] = []
    queue = list(args)
    literal = False
    while queue:
        arg = queue.pop(0)
        if literal:
            words.append(arg)
        elif arg == "--":
            literal = True
        elif arg.startswith("--"):
            key, equals, raw = arg[2:].partition("=")
            negated = by_name.get(key.removeprefix("no-"))
            if variable := by_name.get(key):
                if variable.type == "toggle" and not equals:
                    values[key] = True
                    continue
                if not equals:
                    if not queue:
                        raise FillError(f"--{key} needs a value.")
                    raw = queue.pop(0)
                values[key] = coerce(variable, raw)
            elif key.startswith("no-") and negated and negated.type == "toggle" and not equals:
                values[negated.name] = False
            else:
                known = ", ".join(f"--{name}" for name in by_name) or "none"
                raise FillError(
                    f"This prompt has no variable called {key!r} (it has: {known}). "
                    "Put -- before text that starts with dashes."
                )
        else:
            key, equals, raw = arg.partition("=")
            if equals and key in by_name:
                values[key] = coerce(by_name[key], raw)
            else:
                words.append(arg)

    slots = [variable for variable in positional(variables) if variable.name not in values]
    if words and not slots:
        raise FillError(
            "This prompt has nothing left to fill in, so there's nowhere to put "
            f"{' '.join(words)!r}."
        )
    for i, variable in enumerate(slots):
        if i >= len(words):
            break
        last = i == len(slots) - 1
        values[variable.name] = coerce(variable, " ".join(words[i:]) if last else words[i])
    return values


def missing(variables: Sequence[Variable], values: dict[str, Value]) -> list[Variable]:
    return [v for v in variables if v.required and v.name not in values]


def with_defaults(variables: Sequence[Variable], values: dict[str, Value]) -> dict[str, Value]:
    """Every variable's value: the one given, or else its default."""
    filled = dict(values)
    for variable in variables:
        if variable.name not in filled and variable.default is not None:
            filled[variable.name] = variable.default
    return filled
