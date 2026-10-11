"""A small, predictable template language for prompts.

Three tags, borrowed from Mustache:

    {{name}}                 the value of a variable
    {{#name}} … {{/name}}    kept only when the variable is on (or non-empty)
    {{^name}} … {{/name}}    kept only when it's off (or empty)

Anything else in double braces (say, `${{ github.token }}`) is left alone, and `\\{{` is a
literal `{{`. Values are inserted verbatim and never re-scanned, so clipboard text can't
smuggle tags in.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

TAG = re.compile(r"(\\)?\{\{\s*([#^/]?)\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


@dataclass(frozen=True, slots=True)
class Answer:
    """A yes or no that reads as text of its own when substituted.

    Sections still follow the yes or no, so `{{#bug}}` and `{{bug}}` can both be used.
    """

    yes: bool
    text: str

    def __bool__(self) -> bool:
        return self.yes


Value = str | bool | Answer


class TemplateError(Exception):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


@dataclass(frozen=True, slots=True)
class Var:
    name: str


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    inverted: bool
    children: list[Node] = field(default_factory=list)


Node = str | Var | Section


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def parse(source: str) -> list[Node]:
    root: list[Node] = []
    # Each open section, with the list we return to when it closes and the line it opened on.
    stack: list[tuple[Section, list[Node], int]] = []
    current = root
    pos = 0
    for match in TAG.finditer(source):
        escaped, sigil, name = match.groups()
        start, end = match.span()
        if start < pos:
            continue
        if escaped:
            current.append(source[pos:start] + match.group()[1:])
            pos = end
            continue
        if sigil:
            # A section tag alone on its line shouldn't leave a blank line behind.
            line_start = source.rfind("\n", 0, start) + 1
            newline = source.find("\n", end)
            line_end = len(source) if newline == -1 else newline
            alone = line_start >= pos and not (
                source[line_start:start].strip() or source[end:line_end].strip()
            )
            if alone:
                start, end = line_start, min(line_end + 1, len(source))
        if source[pos:start]:
            current.append(source[pos:start])
        pos = end
        if not sigil:
            current.append(Var(name))
        elif sigil == "/":
            if not stack or stack[-1][0].name != name:
                opened = (
                    f"{{{{#{stack[-1][0].name}}}}} is still open" if stack else "nothing is open"
                )
                raise TemplateError(
                    f"{{{{/{name}}}}} closes a section, but {opened}", _line(source, match.start())
                )
            current = stack.pop()[1]
        else:
            section = Section(name, inverted=sigil == "^")
            current.append(section)
            stack.append((section, current, _line(source, match.start())))
            current = section.children
    if stack:
        section, _, line = stack[-1]
        sigil = "^" if section.inverted else "#"
        raise TemplateError(f"{{{{{sigil}{section.name}}}}} is never closed", line)
    if source[pos:]:
        current.append(source[pos:])
    return root


def _walk(nodes: list[Node]) -> Iterator[Var | Section]:
    for node in nodes:
        if isinstance(node, str):
            continue
        yield node
        if isinstance(node, Section):
            yield from _walk(node.children)


def variables(source: str) -> dict[str, str]:
    """Names used in a template, in order of first appearance, with how each is used.

    "text" for a name that's only ever substituted, "toggle" for one that only switches
    sections on and off, and "optional" for one that does both, as in
    `{{#ticket}}Ticket: {{ticket}}{{/ticket}}`.
    """
    uses: dict[str, set[str]] = {}
    for node in _walk(parse(source)):
        uses.setdefault(node.name, set()).add("value" if isinstance(node, Var) else "section")
    return {
        name: "optional" if len(how) == 2 else "text" if "value" in how else "toggle"
        for name, how in uses.items()
    }


def _show(value: Value) -> str:
    if isinstance(value, Answer):
        return value.text
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def _render(nodes: list[Node], values: Mapping[str, Value]) -> Iterator[str]:
    for node in nodes:
        if isinstance(node, str):
            yield node
            continue
        try:
            value = values[node.name]
        except KeyError:
            raise TemplateError(f"no value for {{{{{node.name}}}}}") from None
        if isinstance(node, Var):
            yield _show(value)
        elif bool(value) != node.inverted:
            yield from _render(node.children, values)


def render(source: str, values: Mapping[str, Value]) -> str:
    """Fill a template in. `values` may be a lazy mapping; only names in use are looked up."""
    return "".join(_render(parse(source), values))
