import threading

import pytest

from pil import config, runner
from pil.kinds import ASK_SYSTEM, Output
from pil.store import parse


def prompt(kind: str, body: str = "Do it.", **front):
    lines = "".join(f"{key}: {value}\n" for key, value in front.items())
    return parse(f"---\nkind: {kind}\n{lines}---\n{body}", "p")


# How every run starts unless something overrides the model or effort.
CLAUDE = ["claude", "--model", "claude-opus-5-5", "--effort", "high"]


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_input_is_attached_in_tags_unless_the_prompt_places_it():
    assert runner.fill(prompt("transform", "Fix this."), {}, "teh text\n") == (
        "Fix this.\n\n<input>\nteh text\n</input>"
    )
    placed = prompt("transform", "Compare:\n{{input}}\nwith {{other}}.")
    assert runner.fill(placed, {"other": "that"}, "this") == "Compare:\nthis\nwith that."


def test_builtins_are_filled_in(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "read_clipboard", lambda: "copied")
    text = runner.fill(prompt("copy", "{{cwd}} {{clipboard}} {{date}}"), {})
    assert text.startswith(f"{tmp_path} copied 20")


def test_a_given_value_beats_a_builtin_of_the_same_name():
    assert runner.fill(prompt("copy", "{{date}}"), {"date": "someday"}) == "someday"


def test_a_session_gets_its_prompt_as_an_argument():
    invocation = runner.prepare(
        prompt("session", "-- Fix {{x}}", mode="plan"), {"x": "it"}, config.Config()
    )
    assert invocation.argv == [*CLAUDE, "--permission-mode", "plan", "--", "-- Fix it"]
    assert invocation.output is None


@pytest.mark.parametrize("kind", ["session", "transform", "ask"])
def test_claude_runs_on_opus_at_high_effort_unless_told_otherwise(kind):
    argv = runner.prepare(prompt(kind), {}, config.Config(), input_text="x").argv
    assert (flag(argv, "--model"), flag(argv, "--effort")) == ("claude-opus-5-5", "high")
    argv = runner.prepare(prompt(kind, model="haiku", effort="low"), {}, config.Config()).argv
    assert (flag(argv, "--model"), flag(argv, "--effort")) == ("haiku", "low")


def test_one_shot_kinds_run_lean_by_default():
    argv = runner.prepare(prompt("ask"), {}, config.Config(), input_text="x").argv
    assert argv[:5] == ["claude", "-p", "--output-format", "json", "--no-session-persistence"]
    assert flag(argv, "--system-prompt") == ASK_SYSTEM
    assert flag(argv, "--tools") == ""
    assert "--strict-mcp-config" in argv
    # The prompt goes in on stdin, not the command line.
    assert "Do it." not in " ".join(argv)


def test_a_prompt_can_turn_the_defaults_back_on():
    fancy = prompt("ask", tools="[Read, Grep]", mcp="true", system="default", effort="high")
    argv = runner.prepare(fancy, {}, config.Config(), input_text="x").argv
    assert flag(argv, "--tools") == "Read,Grep"
    assert flag(argv, "--effort") == "high"
    assert "--strict-mcp-config" not in argv
    assert "--system-prompt" not in argv


def test_options_come_from_kind_then_config_then_prompt_then_the_command_line():
    cfg = config.Config(defaults={"transform": {"model": "haiku", "effort": "low"}})
    plain, picky = prompt("transform"), prompt("transform", model="sonnet")
    assert flag(runner.prepare(plain, {}, cfg, input_text="x").argv, "--model") == "haiku"
    argv = runner.prepare(picky, {}, cfg, input_text="x").argv
    assert (flag(argv, "--model"), flag(argv, "--effort")) == ("sonnet", "low")
    argv = runner.prepare(picky, {}, cfg, input_text="x", overrides={"model": "opus"}).argv
    assert flag(argv, "--model") == "opus"


def test_extra_args_are_passed_through():
    argv = runner.prepare(prompt("session", args="--add-dir ../lib"), {}, config.Config()).argv
    assert argv == [*CLAUDE, "--add-dir", "../lib", "--", "Do it."]


def test_copy_prompts_never_involve_claude():
    invocation = runner.prepare(prompt("copy", "Hi {{who}}"), {"who": "you"}, config.Config())
    assert (invocation.argv, invocation.text) == ([], "Hi you")
    assert invocation.output is Output.CLIPBOARD


def test_broken_prompts_are_not_run():
    with pytest.raises(runner.RunError, match="until it's fixed"):
        runner.prepare(prompt("nonsense"), {}, config.Config())


def test_config_file_sets_the_command_and_per_kind_defaults(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text('claude = "/opt/claude"\n[ask]\nmodel = "sonnet"\n')
    cfg = config.load()
    argv = runner.prepare(prompt("ask"), {}, cfg, input_text="x").argv
    assert argv[0] == "/opt/claude"
    assert flag(argv, "--model") == "sonnet"


def test_a_bad_config_file_is_reported(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text("claude = \n")
    with pytest.raises(config.ConfigError, match=r"config\.toml"):
        config.load()


# ── Actually running ──────────────────────────────────────────────────────────


def job(fake_claude, kind="transform", text="teh text"):
    return runner.Job(runner.prepare(prompt(kind), {}, config.load(), input_text=text))


def test_a_run_returns_claudes_result(fake_claude):
    result = job(fake_claude(result="The text")).run()
    assert (result.text, result.model, result.cost) == ("The text", "claude-test", 0.001)
    assert fake_claude.seen()["prompt"] == "Do it.\n\n<input>\nteh text\n</input>"


def test_an_error_result_raises_with_claudes_explanation(fake_claude):
    with pytest.raises(runner.RunError) as e:
        job(fake_claude(is_error=True, result="No such model.")).run()
    assert e.value.detail == "No such model."


def test_a_crash_raises_with_stderr(fake_claude):
    with pytest.raises(runner.RunError, match="status 3") as e:
        job(fake_claude(exit=3, stderr="not logged in")).run()
    assert e.value.detail == "not logged in"


def test_an_empty_result_is_an_error_not_an_empty_clipboard(fake_claude):
    with pytest.raises(runner.RunError, match="nothing"):
        job(fake_claude(result="  ")).run()


def test_plain_output_is_accepted_as_the_result(fake_claude):
    assert job(fake_claude(stdout="just text")).run().text == "just text"


def test_a_missing_claude_is_explained():
    invocation = runner.prepare(
        prompt("ask"), {}, config.Config(claude="no-such-claude"), input_text="x"
    )
    with pytest.raises(runner.RunError, match="Couldn't find") as e:
        runner.Job(invocation).run()
    assert "config.toml" in (e.value.hint or "")


def test_a_job_can_be_cancelled_from_another_thread(fake_claude, tmp_path):
    fake_claude.path.write_text("#!/bin/sh\nsleep 30\n")
    slow = job(fake_claude)
    threading.Timer(0.2, slow.cancel).start()
    with pytest.raises(runner.Cancelled):
        slow.run()


def test_settle_keeps_a_transforms_trailing_newline():
    transform = runner.prepare(prompt("transform"), {}, config.Config(), input_text="a line\n")
    assert runner.settle(transform, "A line.\n\n") == "A line.\n"
    bare = runner.prepare(prompt("transform"), {}, config.Config(), input_text="a line")
    assert runner.settle(bare, "A line.\n") == "A line."
    ask = runner.prepare(prompt("ask"), {}, config.Config(), input_text="a line\n")
    assert runner.settle(ask, "Answer.\n") == "Answer."


def test_answers_are_replaced_by_the_text_they_stand_for():
    question = parse(
        """\
---
kind: copy
vars:
  bug:
    options: {yes: resolve, no: implement}
  tests:
    default: yes
    options: {no: Skip the tests.}
  size:
    options: {s: small, l: large}
---
Please {{bug}} it ({{size}}).{{#bug}} It's a bug.{{/bug}}
{{tests}}
""",
        "q",
    )
    assert runner.fill(question, {"bug": True, "tests": True, "size": "s"}) == (
        "Please resolve it (small). It's a bug."
    )
    assert runner.fill(question, {"bug": False, "tests": False, "size": "l"}) == (
        "Please implement it (large).\nSkip the tests."
    )
