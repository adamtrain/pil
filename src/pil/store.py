"""Prompts on disk: one Markdown file each, with optional YAML frontmatter.

    ---
    description: Start work on a ticket
    kind: session
    tags: [work]
    vars:
      ticket: The ticket number, like ABC-123
      tests:
        type: toggle
        default: true
    ---

    Start work on {{ticket}}.
    {{#tests}}
    Write failing tests before you change anything.
    {{/tests}}

A prompt's name is its path inside the library without the `.md`, so folders are how prompts
are grouped. Variables don't have to be declared: any `{{name}}` in the body becomes one.
"""

from __future__ import annotations

import contextlib
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import template
from .kinds import DEFAULT_KIND, KINDS, Kind
from .template import Answer, TemplateError, Value

# Filled in by pil itself when a prompt is run, so never asked for.
BUILTINS = {
    "input": "the text the prompt acts on: piped stdin, a file, or else the clipboard",
    "clipboard": "the clipboard's contents",
    "date": "today's date, as YYYY-MM-DD",
    "cwd": "the current directory",
    "branch": "the current git branch",
}

# Frontmatter keys that tune how Claude is run. The config file accepts the same ones.
OPTION_KEYS = ("model", "effort", "mode", "system", "tools", "mcp", "args")

VAR_TYPES = ("text", "toggle", "choice")

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*")

SUFFIX = ".md"


class PromptError(Exception):
    pass


class NotFound(PromptError):
    def __init__(self, query: str, close: list[str]):
        super().__init__(f"No prompt called {query!r}.")
        self.query = query
        self.close = close


class Ambiguous(PromptError):
    def __init__(self, query: str, matches: list[str]):
        super().__init__(f"{query!r} could be any of {len(matches)} prompts.")
        self.query = query
        self.matches = matches


@dataclass(frozen=True, slots=True)
class Variable:
    name: str
    type: str = "text"
    help: str = ""
    default: str | bool | None = None
    options: tuple[str, ...] = ()
    # What each answer stands for, when that isn't the answer itself: for a choice, a text
    # per option; for a toggle, a text for "yes" and one for "no".
    texts: tuple[tuple[str, str], ...] = ()

    @property
    def required(self) -> bool:
        return self.default is None

    def resolve(self, answer: Value) -> Value:
        """Turn an answer into what the prompt should say."""
        texts = dict(self.texts)
        if texts and self.type == "toggle" and isinstance(answer, bool):
            return Answer(answer, texts["yes" if answer else "no"])
        if texts and self.type == "choice" and isinstance(answer, str):
            return texts.get(answer, answer)
        return answer

    @property
    def usage(self) -> str:
        """How this variable looks in a command line synopsis."""
        if self.type == "toggle":
            if self.required:
                return f"--{self.name}|--no-{self.name}"
            return f"[--no-{self.name}]" if self.default else f"[--{self.name}]"
        if self.type == "choice":
            flag = f"--{self.name}={'|'.join(self.options)}"
            return flag if self.required else f"[{flag}]"
        return f"<{self.name}>" if self.required else f"[{self.name}]"


def _answer(key: Any) -> str:
    if isinstance(key, bool):
        return "yes" if key else "no"
    return str(key)


def _variable(name: str, use: str | None, spec: Any) -> Variable:
    if spec is None:
        spec = {}
    elif isinstance(spec, str):
        spec = {"help": spec}
    elif not isinstance(spec, dict):
        raise PromptError(f"vars.{name} should be a description or a table of settings.")
    unknown = set(spec) - {"type", "help", "default", "options"}
    if unknown:
        raise PromptError(f"vars.{name} has unknown settings: {', '.join(sorted(unknown))}.")

    options = spec.get("options") or ()
    texts: tuple[tuple[str, str], ...] = ()
    if isinstance(options, dict):
        # Each answer stands for some text. YAML reads a bare `yes:` or `no:` as true or false.
        texts = tuple(
            (_answer(answer), "" if text is None else str(text)) for answer, text in options.items()
        )
        options = [answer for answer, _ in texts]
    elif not isinstance(options, list | tuple):
        raise PromptError(f"vars.{name}.options should be a list, or a table of answers.")
    options = tuple(str(option) for option in options)
    default = spec.get("default")

    kind = spec.get("type")
    question = bool(texts) and {answer.lower() for answer in options} <= {"yes", "no"}
    if question and kind in (None, "toggle"):
        # A yes-or-no question, where either answer may put some text in the prompt.
        said = {answer.lower(): text for answer, text in texts}
        texts = (("yes", said.get("yes", "")), ("no", said.get("no", "")))
        kind, options = "toggle", ()
    elif kind == "toggle":
        texts = ()
    if kind is None:
        if options:
            kind = "choice"
        elif isinstance(default, bool) or (use == "toggle" and default is None):
            kind = "toggle"
        else:
            kind = "text"
    if kind not in VAR_TYPES:
        raise PromptError(f"vars.{name}.type is {kind!r}; it should be one of {VAR_TYPES}.")

    if kind == "toggle":
        if default is not None and not isinstance(default, bool):
            raise PromptError(f"vars.{name}.default should be true or false.")
        # A question with no default is asked. A plain toggle is simply off.
        default = default if texts and default is None else bool(default)
    elif kind == "choice":
        if not options:
            raise PromptError(f"vars.{name} is a choice, so it needs a list of options.")
        if default is not None and str(default) not in options:
            raise PromptError(f"vars.{name}.default isn't one of its options.")
        default = None if default is None else str(default)
    elif default is not None:
        default = str(default)
    elif use == "optional":
        # Wrapped in its own section, so the prompt already copes with it being empty.
        default = ""
    return Variable(name, kind, str(spec.get("help", "")), default, options, texts)


@dataclass(slots=True)
class Prompt:
    name: str
    body: str = ""
    kind_name: str = DEFAULT_KIND
    description: str = ""
    tags: list[str] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    # Why the file couldn't be understood, if it couldn't. Such prompts can be listed and
    # edited but not run.
    error: str | None = None

    @property
    def kind(self) -> Kind:
        return KINDS.get(self.kind_name, KINDS[DEFAULT_KIND])

    @property
    def folder(self) -> str:
        return self.name.rpartition("/")[0]

    @property
    def title(self) -> str:
        return self.name.rpartition("/")[2]

    @property
    def takes_input(self) -> bool:
        if self.kind.takes_input:
            return True
        with contextlib.suppress(TemplateError):
            return "input" in template.variables(self.body)
        return False

    def variables(self) -> list[Variable]:
        """What needs filling in: declared variables first, then any others the body uses."""
        used = template.variables(self.body)
        names = [*self.vars, *(name for name in used if name not in self.vars)]
        return [
            _variable(name, used.get(name), self.vars.get(name))
            for name in names
            if name not in BUILTINS
        ]

    def check(self) -> None:
        """Raise PromptError if this prompt couldn't be run as written."""
        if not NAME.fullmatch(self.name):
            raise PromptError(
                f"{self.name!r} isn't a usable name. Use letters, digits, '-', '_' and '.', "
                "with '/' between folders."
            )
        if self.kind_name not in KINDS:
            raise PromptError(
                f"kind is {self.kind_name!r}; it should be one of {', '.join(KINDS)}."
            )
        try:
            self.variables()
        except TemplateError as e:
            raise PromptError(f"The prompt's text has a problem on {e}.") from e

    def frontmatter(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.description:
            data["description"] = self.description
        data["kind"] = self.kind_name
        if self.tags:
            data["tags"] = self.tags
        data.update({key: self.options[key] for key in OPTION_KEYS if key in self.options})
        if self.vars:
            data["vars"] = self.vars
        data.update(self.extra)
        return data

    def to_text(self) -> str:
        front = yaml.dump(
            _yes_no_keys(self.frontmatter()),
            Dumper=_Dumper,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )
        return f"---\n{front}---\n\n{self.body.strip()}\n"


class _Dumper(yaml.SafeDumper):
    """Block style for tables, but short lists stay on one line: `tags: [a, b]`."""


class _YesNo(str):
    """A true or false table key, written as `yes` or `no` because that's how questions read."""

    __slots__ = ()


def _yes_no_keys(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    return {
        _YesNo(_answer(key)) if isinstance(key, bool) else key: _yes_no_keys(value)
        for key, value in data.items()
    }


_Dumper.add_representer(
    list,
    lambda dumper, data: dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True),
)
_Dumper.add_representer(
    _YesNo, lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:bool", str(data))
)


def _split(text: str) -> tuple[str, str]:
    """Separate a file into its frontmatter (possibly empty) and body."""
    if not text.startswith("---"):
        return "", text
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return "", text
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    raise PromptError("The frontmatter opens with '---' but never closes.")


def parse(text: str, name: str, path: Path | None = None) -> Prompt:
    """Read a prompt from a file's text. Never raises: problems land in `Prompt.error`."""
    prompt = Prompt(name=name, body=text.strip(), path=path)
    try:
        front, body = _split(text)
        try:
            data = yaml.safe_load(front) if front.strip() else {}
        except yaml.YAMLError as e:
            raise PromptError(f"The frontmatter isn't valid YAML: {e}") from e
        if not isinstance(data, dict):
            raise PromptError("The frontmatter should be a table of settings.")
        prompt.body = body.strip()

        prompt.description = str(data.pop("description", "") or "")
        prompt.kind_name = str(data.pop("kind", DEFAULT_KIND))
        tags = data.pop("tags", None) or []
        prompt.tags = [str(tag) for tag in (tags if isinstance(tags, list) else str(tags).split())]
        declared = data.pop("vars", None) or {}
        if not isinstance(declared, dict):
            raise PromptError("vars should be a table with one entry per variable.")
        prompt.vars = {str(key): value for key, value in declared.items()}
        prompt.options = {key: data.pop(key) for key in OPTION_KEYS if key in data}
        prompt.extra = data
        prompt.check()
    except PromptError as e:
        prompt.error = str(e)
    return prompt


def matches(prompt: Prompt, query: str) -> bool:
    """True when every word of a query appears in a prompt's name, description, kind or tags."""
    haystack = " ".join(
        [prompt.name, prompt.description, prompt.kind_name, *(f"#{tag}" for tag in prompt.tags)]
    ).lower()
    return all(word in haystack for word in query.lower().split())


class Library:
    """A directory of prompts."""

    def __init__(self, root: Path):
        self.root = root

    def path_for(self, name: str) -> Path:
        return self.root / f"{name}{SUFFIX}"

    def all(self) -> list[Prompt]:
        prompts = []
        for path in self.root.rglob(f"*{SUFFIX}"):
            relative = path.relative_to(self.root)
            if any(part.startswith(".") for part in relative.parts) or not path.is_file():
                continue
            name = relative.with_suffix("").as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                prompts.append(Prompt(name=name, path=path, error=f"Couldn't read it: {e}"))
                continue
            prompts.append(parse(text, name, path))
        return sorted(prompts, key=lambda prompt: (prompt.folder, prompt.title))

    def names(self) -> list[str]:
        return [prompt.name for prompt in self.all()]

    def resolve(self, query: str) -> Prompt:
        """Find one prompt by its name, the last part of its name, or an unambiguous prefix."""
        prompts = self.all()
        query = query.removesuffix(SUFFIX).strip("/")
        tiers = (
            lambda p: p.name == query,
            lambda p: p.name.endswith(f"/{query}"),
            lambda p: p.title.startswith(query) or p.name.startswith(query),
        )
        for matches in tiers:
            found = [prompt for prompt in prompts if matches(prompt)]
            if len(found) == 1:
                return found[0]
            if found:
                raise Ambiguous(query, [prompt.name for prompt in found])
        titles = {prompt.title: prompt.name for prompt in prompts}
        names = [prompt.name for prompt in prompts]
        close = difflib.get_close_matches(query, names, n=3)
        close += [titles[t] for t in difflib.get_close_matches(query, list(titles), n=3)]
        raise NotFound(query, list(dict.fromkeys(close)))

    def save(self, prompt: Prompt) -> Prompt:
        """Write a prompt out. If its name changed since it was loaded, the file moves."""
        prompt.check()
        target = self.path_for(prompt.name)
        moving = prompt.path is not None and prompt.path != target
        if target.exists() and (prompt.path is None or moving):
            raise PromptError(f"There's already a prompt called {prompt.name!r}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(prompt.to_text(), encoding="utf-8")
        if moving and prompt.path is not None:
            self._remove(prompt.path)
        prompt.path = target
        prompt.error = None
        return prompt

    def move(self, prompt: Prompt, name: str) -> Prompt:
        """Rename a prompt, leaving the file's contents exactly as they are."""
        if not NAME.fullmatch(name):
            raise PromptError(f"{name!r} isn't a usable name.")
        target = self.path_for(name)
        if prompt.path is None or target == prompt.path:
            return prompt
        if target.exists():
            raise PromptError(f"There's already a prompt called {name!r}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = prompt.path
        source.rename(target)
        self._prune(source.parent)
        prompt.name, prompt.path = name, target
        return prompt

    def replace_body(self, prompt: Prompt, body: str) -> Prompt:
        """Swap a prompt's text for another, leaving its frontmatter exactly as written."""
        if prompt.path is None:
            raise PromptError(f"{prompt.name} hasn't been saved yet.")
        text = prompt.path.read_text(encoding="utf-8", errors="replace")
        old = _split(text)[1]
        head = text[: len(text) - len(old)]
        text = f"{head}\n{body.strip()}\n" if head else f"{body.strip()}\n"
        replaced = parse(text, prompt.name, prompt.path)
        if replaced.error:
            raise PromptError(replaced.error)
        prompt.path.write_text(text, encoding="utf-8")
        return replaced

    def copy(self, prompt: Prompt, name: str) -> Prompt:
        """Duplicate a prompt's file under another name."""
        if not NAME.fullmatch(name):
            raise PromptError(f"{name!r} isn't a usable name.")
        target = self.path_for(name)
        if prompt.path is None or target.exists():
            raise PromptError(f"There's already a prompt called {name!r}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        text = prompt.path.read_text(encoding="utf-8", errors="replace")
        target.write_text(text, encoding="utf-8")
        return parse(text, name, target)

    def delete(self, prompt: Prompt) -> None:
        if prompt.path is not None:
            self._remove(prompt.path)

    def _remove(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self._prune(path.parent)

    def _prune(self, folder: Path) -> None:
        """Remove folders left empty, up to but not including the library itself."""
        while folder != self.root and self.root in folder.parents:
            try:
                folder.rmdir()
            except OSError:
                return
            folder = folder.parent
