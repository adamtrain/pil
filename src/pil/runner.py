"""Fill a prompt in and hand it to Claude."""

from __future__ import annotations

import contextlib
import datetime
import functools
import json
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import template
from .clipboard import read_clipboard
from .config import Config
from .kinds import Output, Runner
from .store import Prompt
from .template import Value


class RunError(Exception):
    def __init__(self, message: str, *, detail: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.detail = detail
        self.hint = hint


class Cancelled(Exception):
    pass


class Context(Mapping[str, Value]):
    """Values for a template: the ones given, plus built-ins worked out only if used."""

    def __init__(self, values: Mapping[str, Value], input_text: str | None = None):
        self.given = values
        self.input_text = input_text

    def __getitem__(self, name: str) -> Value:
        if name in self.given:
            return self.given[name]
        match name:
            case "input":
                return self.input_text or ""
            case "clipboard":
                return read_clipboard()
            case "date":
                return datetime.date.today().isoformat()
            case "cwd":
                return str(Path.cwd())
            case "branch":
                return _git_branch()
        raise KeyError(name)

    def __iter__(self):
        return iter(self.given)

    def __len__(self) -> int:
        return len(self.given)


@functools.cache
def _git_branch() -> str:
    try:
        done = subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def fill(prompt: Prompt, values: Mapping[str, Value], input_text: str | None = None) -> str:
    """The prompt's text with its variables filled in and its input, if any, attached."""
    resolved = {
        variable.name: variable.resolve(values[variable.name])
        for variable in prompt.variables()
        if variable.name in values
    }
    text = template.render(prompt.body, Context({**values, **resolved}, input_text)).strip()
    if input_text is not None and "input" not in template.variables(prompt.body):
        text = f"{text}\n\n<input>\n{input_text.strip('\n')}\n</input>"
    return text


def options_for(
    prompt: Prompt, config: Config, overrides: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Settle the options for a run: the kind's, then the config file's, then the prompt's."""
    options = dict(prompt.kind.defaults)
    options.update(config.defaults.get(prompt.kind.name, {}))
    options.update(prompt.options)
    options.update({key: value for key, value in (overrides or {}).items() if value is not None})
    return options


def _words(value: Any) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    return [str(item) for item in value]


def command(
    prompt: Prompt, options: Mapping[str, Any], text: str, claude: str = "claude"
) -> list[str]:
    """The claude command line for a run. One-shot runs get their prompt on stdin instead."""
    runner = prompt.kind.runner
    if runner is Runner.NONE:
        return []
    argv = [claude]
    if runner is Runner.PRINT:
        argv += ["-p", "--output-format", "json", "--no-session-persistence"]
    for key, flag in (("model", "--model"), ("effort", "--effort"), ("mode", "--permission-mode")):
        if options.get(key):
            argv += [flag, str(options[key])]
    system = options.get("system")
    if system and system != "default":
        argv += ["--system-prompt", str(system)]
    tools = options.get("tools")
    if tools in ("none", False) or tools == []:
        argv += ["--tools", ""]
    elif tools and tools != "default":
        argv += ["--tools", tools if isinstance(tools, str) else ",".join(map(str, tools))]
    if options.get("mcp") is False:
        argv.append("--strict-mcp-config")
    argv += _words(options.get("args") or [])
    if runner is Runner.SESSION:
        argv += ["--", text]
    return argv


@dataclass(frozen=True, slots=True)
class Invocation:
    """A prompt that's ready to run."""

    prompt: Prompt
    text: str
    argv: list[str]
    output: Output | None
    input_text: str | None = None

    @property
    def runner(self) -> Runner:
        return self.prompt.kind.runner


def prepare(
    prompt: Prompt,
    values: Mapping[str, Value],
    config: Config,
    *,
    input_text: str | None = None,
    output: Output | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Invocation:
    if prompt.error:
        raise RunError(f"{prompt.name} can't be run until it's fixed.", detail=prompt.error)
    try:
        text = fill(prompt, values, input_text)
    except template.TemplateError as e:
        raise RunError(f"Couldn't fill in {prompt.name}: {e}.") from e
    argv = command(prompt, options_for(prompt, config, overrides), text, config.claude)
    return Invocation(prompt, text, argv, output or prompt.kind.output, input_text)


@dataclass(frozen=True, slots=True)
class Result:
    text: str
    seconds: float
    cost: float | None = None
    model: str | None = None


def _parse(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


class Job:
    """One `claude -p` run, which another thread may cancel."""

    def __init__(self, invocation: Invocation):
        self.invocation = invocation
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._process is not None and self._process.poll() is None:
                # The whole group, so nothing claude started is left holding the pipes open.
                with contextlib.suppress(OSError):
                    if hasattr(os, "killpg"):
                        os.killpg(self._process.pid, signal.SIGTERM)
                    else:
                        self._process.terminate()

    def run(self) -> Result:
        argv = self.invocation.argv
        started = time.monotonic()
        if not shutil.which(argv[0]):
            raise RunError(
                f"Couldn't find the {argv[0]!r} command.",
                hint="Install Claude Code (https://claude.com/claude-code), or point pil at it "
                "with [bold]claude = …[/] in config.toml.",
            )
        with self._lock:
            if self._cancelled:
                raise Cancelled
            try:
                self._process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                )
            except OSError as e:
                raise RunError(f"Couldn't start {argv[0]}: {e.strerror or e}") from e
        stdout, stderr = self._process.communicate(self.invocation.text)
        if self._cancelled:
            raise Cancelled
        seconds = time.monotonic() - started

        data = _parse(stdout)
        if data is None:
            if self._process.returncode != 0:
                detail = (stderr.strip() or stdout.strip())[-2000:] or None
                raise RunError(
                    f"claude exited with status {self._process.returncode}.", detail=detail
                )
            return Result(stdout.strip("\n"), seconds)
        text = str(data.get("result") or "")
        if data.get("is_error") or self._process.returncode != 0:
            raise RunError("Claude couldn't finish that.", detail=text or stderr.strip() or None)
        if not text.strip():
            raise RunError("Claude came back with nothing.")
        models = list(data.get("modelUsage") or {})
        return Result(text, seconds, data.get("total_cost_usd"), models[0] if models else None)


def settle(invocation: Invocation, text: str) -> str:
    """Tidy a result on its way out: a transform keeps its input's trailing newline."""
    text = text.strip("\n")
    source = invocation.input_text
    if invocation.output is Output.CLIPBOARD and source and source.endswith("\n"):
        text += "\n"
    return text
