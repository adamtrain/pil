import io

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from pil import cli
from pil.examples import EXAMPLES

runner = CliRunner()


def pil(*args: str, input: str | None = None):
    return runner.invoke(cli.app, list(args), input=input)


@pytest.fixture(autouse=True)
def stdin_is_a_terminal(monkeypatch):
    """Unless a test says text was piped in, behave as if nothing was."""
    state = {"piped": False}
    monkeypatch.setattr(cli, "_stdin_is_piped", lambda stdin: state["piped"])
    return state


@pytest.fixture
def tty(monkeypatch):
    """Make pil believe stdout is a terminal, as it is when someone runs it by hand."""
    plain = Console(force_terminal=True, color_system=None, highlight=False, width=100)
    monkeypatch.setattr(cli, "out", plain)


@pytest.fixture
def prompts(write):
    write(
        "work/ticket",
        """\
        ---
        description: Start work on a ticket
        vars:
          ticket: The ticket number
          tests:
            type: toggle
            default: true
        ---
        Work on {{ticket}}.
        {{#tests}}
        Write tests first.
        {{/tests}}
        """,
    )
    write("text/fix", "---\nkind: transform\ndescription: Fix typos\ntags: [writing]\n---\nFix it.")
    write("ask/explain", "---\nkind: ask\n---\n{{#question}}{{question}}{{/question}}")
    write("copy/sig", "---\nkind: copy\n---\nRegards, {{name}}")


# ── Finding and filling ───────────────────────────────────────────────────────


def test_a_prompts_name_is_a_command(prompts):
    result = pil("-n", "ticket", "ABC-1")
    assert result.exit_code == 0
    assert result.stdout == "Work on ABC-1.\nWrite tests first.\n"
    assert pil("run", "-n", "work/ticket", "ABC-1").stdout == result.stdout


def test_pils_options_work_after_the_name_too(prompts):
    result = pil("ticket", "ABC-1", "--no-tests", "-n")
    assert result.stdout == "Work on ABC-1.\n"
    assert "claude" in result.stderr


def test_a_prompts_own_variable_beats_an_option_of_the_same_name(write):
    write("compare", "Compare with {{model}}.")
    assert pil("-n", "compare", "--model", "gpt").stdout == "Compare with gpt.\n"


def test_missing_values_are_an_error_when_nobody_can_be_asked(prompts):
    result = pil("ticket")
    assert result.exit_code == 2
    assert "still needs: ticket" in result.stderr
    assert "<ticket> [--no-tests]" in result.stderr


def test_bad_arguments_show_the_prompts_usage(prompts):
    result = pil("ticket", "ABC-1", "--bogus")
    assert result.exit_code == 2
    assert "no variable called 'bogus'" in result.stderr
    assert "pil work/ticket <ticket> [--no-tests]" in result.stderr


def test_each_prompt_has_its_own_help(prompts):
    result = pil("ticket", "-h")
    assert result.exit_code == 0
    assert "Start work on a ticket" in result.stdout
    assert "pil work/ticket <ticket> [--no-tests]" in result.stdout
    assert "The ticket number" in result.stdout
    assert "Work on {{ticket}}" not in result.stdout


def test_unknown_names_get_suggestions(prompts):
    result = pil("tikcet")
    assert result.exit_code == 1
    assert "No prompt called 'tikcet'" in result.stderr
    assert "work/ticket" in result.stderr


def test_ambiguous_names_list_the_candidates(prompts, write):
    write("personal/ticket", "x")
    result = pil("ticket")
    assert result.exit_code == 1
    assert "personal/ticket, work/ticket" in result.stderr


def test_broken_prompts_say_what_is_wrong(write):
    write("bad", "---\nkind: telepathy\n---\nHm.")
    result = pil("bad")
    assert result.exit_code == 1
    assert "kind is 'telepathy'" in result.stderr
    assert "pil edit bad" in result.stderr


# ── Running ───────────────────────────────────────────────────────────────────


def test_a_transform_rewrites_the_clipboard(prompts, fake_claude, clipboard, tty):
    clipboard["text"] = "teh text\n"
    fake_claude(result="The text")
    result = pil("fix")
    assert result.exit_code == 0
    assert clipboard["text"] == "The text\n"
    assert "Clipboard updated" in result.stderr
    assert "2 → 2 words" in result.stderr
    assert fake_claude.seen()["prompt"] == "Fix it.\n\n<input>\nteh text\n</input>"
    assert "The text" not in result.stdout


def test_piped_text_is_used_instead_of_the_clipboard(
    prompts, fake_claude, clipboard, tty, stdin_is_a_terminal
):
    stdin_is_a_terminal["piped"] = True
    clipboard["text"] = "not this"
    pil("fix", input="from a pipe")
    assert "from a pipe" in fake_claude.seen()["prompt"]
    assert clipboard["text"] == "FIX IT.\n\n<INPUT>\nFROM A PIPE\n</INPUT>"


def test_redirected_output_gets_the_result_and_the_clipboard_is_left_alone(
    prompts, fake_claude, clipboard
):
    clipboard["text"] = "teh text"
    fake_claude(result="The text")
    result = pil("fix")
    assert result.stdout == "The text\n"
    assert clipboard["text"] == "teh text"


def test_output_can_be_sent_elsewhere(prompts, fake_claude, clipboard, tty):
    clipboard["text"] = "teh text"
    fake_claude(result="The text")
    assert pil("fix", "-o", "terminal").stdout == "The text\n"
    assert clipboard["text"] == "teh text"
    pil("-o", "clipboard", "explain")
    assert clipboard["text"] == "The text"


def test_an_ask_prints_the_answer(prompts, fake_claude, clipboard, tty):
    clipboard["text"] = "def f(): ..."
    fake_claude(result="It's a **stub**.")
    result = pil("explain", "what", "is", "this")
    assert "It's a stub." in result.stdout
    assert fake_claude.seen()["prompt"].startswith("what is this\n\n<input>")
    assert clipboard["text"] == "def f(): ..."
    assert pil("explain", "--raw").stdout == "It's a **stub**.\n"


def test_a_file_can_stand_in_for_the_clipboard(prompts, fake_claude, clipboard, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("from a file")
    pil("explain", "-f", str(notes))
    assert "from a file" in fake_claude.seen()["prompt"]
    result = pil("explain", "-f", str(tmp_path / "missing.md"))
    assert result.exit_code == 1
    assert "Couldn't read" in result.stderr


def test_an_empty_clipboard_stops_the_run(prompts, fake_claude, clipboard):
    clipboard["text"] = "  \n"
    result = pil("fix")
    assert result.exit_code == 1
    assert "clipboard is empty" in result.stderr


def test_when_claude_fails_the_clipboard_is_untouched(prompts, fake_claude, clipboard, tty):
    clipboard["text"] = "precious"
    fake_claude(is_error=True, result="Overloaded.")
    result = pil("fix")
    assert result.exit_code == 1
    assert "Overloaded." in result.stderr
    assert clipboard["text"] == "precious"


def test_a_copy_prompt_goes_to_the_clipboard_without_claude(prompts, clipboard, tty):
    result = pil("sig", "Adam")
    assert result.exit_code == 0
    assert clipboard["text"] == "Regards, Adam"
    assert "Copied to the clipboard" in result.stderr


def test_a_session_prompt_hands_over_to_claude(prompts, fake_claude, monkeypatch):
    launched = []

    def execvp(file, argv):
        launched.append((file, argv))
        raise typer.Exit()

    monkeypatch.setattr(cli.os, "execvp", execvp)
    assert pil("ticket", "ABC-1", "-m", "opus").exit_code == 0
    claude = str(fake_claude.path)
    assert launched == [
        (
            claude,
            [
                claude,
                "--model",
                "opus",
                "--effort",
                "high",
                "--",
                "Work on ABC-1.\nWrite tests first.",
            ],
        )
    ]


def test_a_session_has_no_output_to_redirect(prompts):
    result = pil("ticket", "ABC-1", "-o", "clipboard")
    assert result.exit_code == 1
    assert "starts a session" in result.stderr


# ── The library ───────────────────────────────────────────────────────────────


def test_ls_is_tab_separated_when_piped(prompts):
    lines = pil("ls").stdout.splitlines()
    assert "text/fix\ttransform\tFix typos" in lines
    assert len(lines) == 4
    assert pil("ls", "-k", "ask").stdout == "ask/explain\task\t\n"
    assert pil("ls", "-t", "writing").stdout.startswith("text/fix")
    assert pil("ls", "work", "tick").stdout.startswith("work/ticket")


def test_ls_is_a_table_at_a_terminal(prompts, tty):
    result = pil("ls")
    assert "<ticket> [--no-tests]" in result.stdout
    assert "Start work on a ticket" in result.stdout


def test_ls_points_the_way_when_the_library_is_empty(tty):
    assert "pil examples" in pil("ls").stderr


def test_show_prints_the_file_when_piped(prompts, library):
    assert pil("show", "fix").stdout == library.path_for("text/fix").read_text()


def test_show_describes_the_prompt_at_a_terminal(prompts, tty):
    result = pil("show", "ticket")
    assert "Work on {{ticket}}." in result.stdout
    assert "The ticket number" in result.stdout


def test_new_takes_its_body_from_a_pipe(library, stdin_is_a_terminal):
    stdin_is_a_terminal["piped"] = True
    result = pil("new", "text/shorten", "-k", "transform", "-d", "Cut it down", input="Shorten.\n")
    assert result.exit_code == 0, result.output
    prompt = library.resolve("shorten")
    assert (prompt.kind.name, prompt.description, prompt.body) == (
        "transform",
        "Cut it down",
        "Shorten.",
    )


def test_new_refuses_bad_kinds_and_taken_names(prompts):
    assert pil("new", "x", "-k", "telepathy").exit_code == 2
    result = pil("new", "text/fix", "--no-edit")
    assert result.exit_code == 1
    assert "already a prompt" in result.stderr


def test_mv_and_rm(prompts, library):
    assert pil("mv", "fix", "writing/fix-typos").exit_code == 0
    assert "writing/fix-typos" in library.names()
    assert pil("rm", "-y", "fix-typos", "sig").exit_code == 0
    assert library.names() == ["ask/explain", "work/ticket"]
    assert pil("rm", "explain", input="n\n").exit_code == 0
    assert "ask/explain" in library.names()


def test_edit_opens_the_file_and_checks_it_afterwards(prompts, library, monkeypatch, tmp_path):
    editor = tmp_path / "editor"
    editor.write_text('#!/bin/sh\nprintf -- "---\\nkind: telepathy\\n---\\nHm.\\n" > "$1"\n')
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    result = pil("edit", "fix")
    assert result.exit_code == 1
    assert "it has a problem" in result.stderr
    assert "telepathy" in library.path_for("text/fix").read_text()


def test_dir_prints_where_prompts_live(library):
    assert pil("dir").stdout.strip() == str(library.root)


def test_examples_install_once_and_all_of_them_work(library):
    assert pil("examples").exit_code == 0
    prompts = library.all()
    assert [prompt.name for prompt in prompts if prompt.error] == []
    assert {prompt.name for prompt in prompts} == set(EXAMPLES)
    library.path_for("text/fix").write_text("Mine now.")
    assert "already there" in pil("examples").stderr
    assert library.path_for("text/fix").read_text() == "Mine now."


def test_the_examples_fill_in_cleanly(library, clipboard):
    pil("examples")
    result = pil("-n", "start/review", "--focus", "the cache")
    assert "Pay particular attention to the cache." in result.stdout
    assert "\n\n\n" not in result.stdout
    result = pil("-n", "bug-report", "the", "build", "is", "broken", "--severity=high")
    assert "**Summary:** the build is broken\n**Severity:** high" in result.stdout


# ── Odds and ends ─────────────────────────────────────────────────────────────


def test_version_and_help():
    assert pil("--version").stdout.startswith("pil ")
    assert "opens the TUI" in pil("--help").stdout
    # Without a terminal there's no TUI to open, so say how pil works instead.
    assert "Usage" in pil().stdout


def test_completion_offers_names_a_shell_can_match(prompts, write):
    assert [name for name, _ in cli._complete_names("ti")] == ["ticket"]
    assert [name for name, _ in cli._complete_names("work/")] == ["work/ticket"]
    write("personal/ticket", "x")
    # Two prompts share the short name now, so only full names are offered.
    assert cli._complete_names("ti") == []


def test_resolve_input_prefers_file_then_pipe_then_clipboard(
    clipboard, tmp_path, stdin_is_a_terminal
):
    clipboard["text"] = "clip"
    assert cli.resolve_input(None, io.StringIO("")) == ("clip", "clipboard")
    stdin_is_a_terminal["piped"] = True
    assert cli.resolve_input(None, io.StringIO("piped")) == ("piped", "stdin")
    note = tmp_path / "n.md"
    note.write_text("file")
    assert cli.resolve_input(note, io.StringIO("piped")) == ("file", "n.md")


# ── Questions ─────────────────────────────────────────────────────────────────


@pytest.fixture
def question(write):
    write(
        "triage",
        """\
        ---
        vars:
          bug:
            help: Is this ticket a bug?
            options:
              yes: resolve
              no: implement
          tests:
            default: yes
            options:
              no: Skip the tests.
        ---
        Please {{bug}} {{ticket}}.
        {{tests}}
        """,
    )


def test_a_question_resolves_to_the_text_for_its_answer(question):
    assert pil("-n", "triage", "ABC-1", "--bug").stdout == "Please resolve ABC-1.\n"
    assert pil("-n", "triage", "ABC-1", "--no-bug", "--no-tests").stdout == (
        "Please implement ABC-1.\nSkip the tests.\n"
    )


def test_an_unanswered_question_is_asked_for(question):
    result = pil("-n", "triage", "ABC-1")
    assert result.exit_code == 2
    assert "still needs: bug" in result.stderr
    assert "--bug|--no-bug" in result.stderr


def test_a_prompts_help_shows_what_each_answer_stands_for(question):
    result = pil("triage", "-h")
    assert "Is this ticket a bug?" in result.stdout
    assert "yes → resolve" in result.stdout
    assert "no → implement" in result.stdout
    assert "no → Skip the tests." in result.stdout
