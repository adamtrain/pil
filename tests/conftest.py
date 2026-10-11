import json
import stat
import sys
import textwrap

import pytest

from pil import config
from pil.store import Library


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point pil at an empty home so tests never touch the real library."""
    monkeypatch.setenv("PIL_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def library(home) -> Library:
    return Library(config.prompts_dir())


@pytest.fixture
def write(library):
    """Put a prompt file in the library: write("work/ticket", "---\\nkind: ask\\n---\\nBody")."""

    def write(name: str, text: str):
        path = library.path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    return write


FAKE_CLAUDE = """\
#!{python}
import json, sys

args = sys.argv[1:]
prompt = sys.stdin.read() if "-p" in args else args[-1]
with open({log!r}, "a") as log:
    log.write(json.dumps({{"args": args, "prompt": prompt}}) + "\\n")
reply = {reply!r}
if "results" in reply:
    call = sum(1 for _ in open({log!r})) - 1
    reply["result"] = reply["results"][min(call, len(reply["results"]) - 1)]
if reply.get("exit"):
    sys.stderr.write(reply.get("stderr", ""))
    sys.exit(reply["exit"])
if "stdout" in reply:
    print(reply["stdout"])
else:
    print(json.dumps({{
        "type": "result",
        "is_error": reply.get("is_error", False),
        "result": reply.get("result", prompt.upper()),
        "total_cost_usd": 0.001,
        "modelUsage": {{"claude-test": {{}}}},
    }}))
"""


@pytest.fixture
def fake_claude(home, tmp_path):
    """Install a stand-in for the claude command and tell pil to use it.

    By default it answers with the prompt in capitals. Call the fixture with a reply to
    change that (`results=[…]` gives one answer per call, repeating the last), and read
    `.seen()` for the arguments and prompt it was last given or `.calls()` for all of them.
    """
    script = tmp_path / "claude"
    log = tmp_path / "claude.jsonl"

    class Fake:
        path = script

        def __call__(self, **reply):
            log.unlink(missing_ok=True)
            script.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log), reply=reply))
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            return self

        def calls(self) -> list[dict]:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

        def seen(self) -> dict:
            return self.calls()[-1]

    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(f'claude = "{script}"\n')
    return Fake()()


@pytest.fixture
def clipboard(monkeypatch):
    """Swap the real clipboard for a dict, wherever pil reaches for it."""
    from pil import cli, runner
    from pil.tui import app, run

    contents = {"text": ""}
    for module in (cli, runner, app, run):
        if hasattr(module, "read_clipboard"):
            monkeypatch.setattr(module, "read_clipboard", lambda: contents["text"])
        if hasattr(module, "write_clipboard"):
            monkeypatch.setattr(
                module, "write_clipboard", lambda text: contents.__setitem__("text", text)
            )
    return contents
