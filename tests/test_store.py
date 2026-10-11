import pytest

from pil.store import Ambiguous, NotFound, Prompt, PromptError, matches, parse
from pil.template import Answer

TICKET = """\
---
description: Start work on a ticket
kind: session
tags: [work, jira]
mode: plan
owner: adam
vars:
  ticket: The ticket number
  tests:
    type: toggle
    default: true
  scope:
    options: [small, large]
    default: small
---

Work on {{ticket}} ({{scope}}).
{{#tests}}
Write tests.
{{/tests}}
{{#notes}}Notes: {{notes}}{{/notes}}
"""


def variables(prompt):
    return {v.name: v for v in prompt.variables()}


def test_parse_reads_frontmatter_and_body():
    prompt = parse(TICKET, "work/ticket")
    assert prompt.error is None
    assert prompt.description == "Start work on a ticket"
    assert prompt.kind.name == "session"
    assert prompt.tags == ["work", "jira"]
    assert prompt.options == {"mode": "plan"}
    assert prompt.extra == {"owner": "adam"}
    assert prompt.body.startswith("Work on {{ticket}}")
    assert (prompt.folder, prompt.title) == ("work", "ticket")


def test_a_file_without_frontmatter_is_all_prompt():
    prompt = parse("Just do {{thing}}.\n", "plain")
    assert prompt.error is None
    assert prompt.kind.name == "session"
    assert prompt.body == "Just do {{thing}}."


def test_declared_variables_come_first_then_the_rest_in_order_of_use():
    found = variables(parse(TICKET, "t"))
    assert list(found) == ["ticket", "tests", "scope", "notes"]
    assert (found["ticket"].type, found["ticket"].required) == ("text", True)
    assert found["ticket"].help == "The ticket number"
    assert (found["tests"].type, found["tests"].default) == ("toggle", True)
    assert (found["scope"].type, found["scope"].options) == ("choice", ("small", "large"))
    assert found["scope"].default == "small"


def test_undeclared_variables_are_inferred_from_how_they_are_used():
    found = variables(parse("{{who}} {{#loud}}!{{/loud}} {{#why}}{{why}}{{/why}}", "t"))
    assert (found["who"].type, found["who"].required) == ("text", True)
    assert (found["loud"].type, found["loud"].default) == ("toggle", False)
    # Wrapped in its own section, so the prompt copes with it being empty.
    assert (found["why"].type, found["why"].default) == ("text", "")


def test_builtins_are_never_asked_for():
    prompt = parse("On {{branch}} at {{date}} in {{cwd}}: {{clipboard}} {{input}} {{x}}", "t")
    assert list(variables(prompt)) == ["x"]
    assert prompt.takes_input


def test_only_some_kinds_take_input_by_themselves():
    assert parse("---\nkind: transform\n---\nFix it.", "t").takes_input
    assert parse("---\nkind: ask\n---\nWhy?", "t").takes_input
    assert not parse("---\nkind: session\n---\nGo.", "t").takes_input
    assert not parse("---\nkind: copy\n---\nText.", "t").takes_input


def test_usage_shows_how_each_variable_is_given():
    found = variables(parse(TICKET, "t"))
    assert found["ticket"].usage == "<ticket>"
    assert found["notes"].usage == "[notes]"
    assert found["tests"].usage == "[--no-tests]"
    assert found["scope"].usage == "[--scope=small|large]"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("---\nkind: [unclosed\n---\nBody", "isn't valid YAML"),
        ("---\nkind: session\nBody", "never closes"),
        ("---\nkind: telepathy\n---\nBody", "kind is 'telepathy'"),
        ("---\nvars:\n  x:\n    type: number\n---\n{{x}}", "vars.x.type"),
        ("---\nvars:\n  x:\n    type: choice\n---\n{{x}}", "needs a list of options"),
        ("---\nvars:\n  x:\n    options: [a]\n    default: b\n---\n{{x}}", "isn't one of"),
        ("---\nvars:\n  x:\n    colour: red\n---\n{{x}}", "unknown settings: colour"),
        ("{{#open}}never shut", "never closed"),
    ],
)
def test_broken_files_load_with_an_error_instead_of_raising(text, problem):
    prompt = parse(text, "broken")
    assert prompt.error is not None
    assert problem in prompt.error


def test_writing_a_prompt_out_and_reading_it_back_changes_nothing():
    prompt = parse(TICKET, "work/ticket")
    again = parse(prompt.to_text(), "work/ticket")
    assert again.error is None
    assert again.to_text() == prompt.to_text()
    assert again.frontmatter() == prompt.frontmatter()
    assert "tags: [work, jira]" in prompt.to_text()


def test_matches_looks_at_name_description_kind_and_tags():
    prompt = parse(TICKET, "work/ticket")
    assert matches(prompt, "")
    assert matches(prompt, "tick")
    assert matches(prompt, "start session")
    assert matches(prompt, "#jira work")
    assert not matches(prompt, "ticket transform")


# ── The library ───────────────────────────────────────────────────────────────


def test_all_lists_prompts_by_folder_and_skips_hidden_files(library, write):
    write("zebra", "z")
    write("work/ticket", "t")
    write("work/deep/nested", "n")
    write(".git/config", "not a prompt")
    (library.root / "notes.txt").write_text("not a prompt either")
    assert library.names() == ["zebra", "work/ticket", "work/deep/nested"]


def test_an_empty_or_missing_library_is_just_empty(library):
    assert library.all() == []


def test_resolve_accepts_full_names_last_parts_and_unique_prefixes(library, write):
    for name in ("work/ticket", "work/review", "text/fix", "fix-up"):
        write(name, "x")
    assert library.resolve("work/ticket").name == "work/ticket"
    assert library.resolve("ticket").name == "work/ticket"
    assert library.resolve("tick").name == "work/ticket"
    assert library.resolve("ticket.md").name == "work/ticket"
    # An exact last part beats a prefix of something else.
    assert library.resolve("fix").name == "text/fix"


def test_resolve_refuses_to_guess(library, write):
    for name in ("work/review", "personal/review", "text/rewrite"):
        write(name, "x")
    with pytest.raises(Ambiguous) as e:
        library.resolve("review")
    assert e.value.matches == ["personal/review", "work/review"]
    with pytest.raises(Ambiguous):
        library.resolve("re")


def test_resolve_suggests_near_misses(library, write):
    write("work/ticket", "x")
    with pytest.raises(NotFound) as e:
        library.resolve("tikcet")
    assert e.value.close == ["work/ticket"]


def test_save_creates_folders_and_refuses_to_overwrite_another_prompt(library):
    saved = library.save(Prompt(name="a/b/c", body="Hello {{who}}."))
    assert saved.path == library.root / "a/b/c.md"
    assert library.resolve("c").body == "Hello {{who}}."
    with pytest.raises(PromptError, match="already a prompt"):
        library.save(Prompt(name="a/b/c", body="Other."))


def test_saving_under_a_new_name_moves_the_file_and_tidies_up(library):
    prompt = library.save(Prompt(name="old/place", body="x"))
    prompt.name = "new/place"
    library.save(prompt)
    assert library.names() == ["new/place"]
    assert not (library.root / "old").exists()


@pytest.mark.parametrize("name", ["", "/abs", "a//b", "../escape", "a/../b", "has space", "-dash"])
def test_unusable_names_are_refused(library, name):
    with pytest.raises(PromptError, match="usable name"):
        library.save(Prompt(name=name, body="x"))
    assert library.all() == []


def test_move_keeps_the_file_exactly_as_written(library, write):
    original = "---\nkind: ask\n# a comment worth keeping\n---\nWhy?\n"
    write("q", original)
    moved = library.move(library.resolve("q"), "asks/why")
    assert moved.path.read_text() == original
    assert library.names() == ["asks/why"]


def test_move_and_copy_refuse_taken_names(library, write):
    write("a", "x")
    write("b", "y")
    with pytest.raises(PromptError, match="already"):
        library.move(library.resolve("a"), "b")
    with pytest.raises(PromptError, match="already"):
        library.copy(library.resolve("a"), "b")
    assert library.copy(library.resolve("a"), "c").body == "x"


def test_delete_removes_the_file_and_empty_folders_but_not_the_library(library, write):
    write("only/one", "x")
    library.delete(library.resolve("one"))
    assert library.all() == []
    assert library.root.exists()


# ── Answers that stand for text ───────────────────────────────────────────────

QUESTION = """\
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
  size:
    options:
      s: Keep it small.
      l: Tidy as you go.
    default: s
---
{{bug}} {{tests}} {{size}}
"""


def test_yes_and_no_options_make_a_question_that_is_asked():
    bug = variables(parse(QUESTION, "q"))["bug"]
    assert (bug.type, bug.required, bug.options) == ("toggle", True, ())
    assert bug.texts == (("yes", "resolve"), ("no", "implement"))
    assert bug.usage == "--bug|--no-bug"
    assert bug.resolve(True) == Answer(True, "resolve")
    assert bug.resolve(False) == Answer(False, "implement")


def test_a_question_with_a_default_is_only_a_flag_and_either_text_may_be_left_out():
    tests = variables(parse(QUESTION, "q"))["tests"]
    assert (tests.type, tests.default, tests.usage) == ("toggle", True, "[--no-tests]")
    assert tests.resolve(True) == Answer(True, "")
    assert tests.resolve(False) == Answer(False, "Skip the tests.")


def test_a_choice_can_give_each_option_a_text():
    size = variables(parse(QUESTION, "q"))["size"]
    assert (size.type, size.options, size.default) == ("choice", ("s", "l"), "s")
    assert size.resolve("l") == "Tidy as you go."
    assert size.usage == "[--size=s|l]"


def test_plain_toggles_and_choices_resolve_to_themselves():
    found = variables(parse(TICKET, "t"))
    assert found["tests"].resolve(False) is False
    assert found["scope"].resolve("large") == "large"
    assert found["ticket"].resolve("ABC-1") == "ABC-1"


def test_a_question_is_written_back_out_with_yes_and_no():
    prompt = parse(QUESTION, "q")
    text = prompt.to_text()
    assert "      yes: resolve\n      no: implement\n" in text
    assert "    default: true\n" in text
    again = parse(text, "q")
    assert again.error is None
    assert again.variables() == prompt.variables()


def test_replacing_the_body_leaves_the_frontmatter_exactly_as_written(library, write):
    write(
        "q", "---\nkind: ask\n# a comment worth keeping\nmodel:   haiku\n---\n\n\nWhy is it so?\n"
    )
    replaced = library.replace_body(library.resolve("q"), "Why?\n\n")
    assert replaced.body == "Why?"
    assert library.path_for("q").read_text() == (
        "---\nkind: ask\n# a comment worth keeping\nmodel:   haiku\n---\n\nWhy?\n"
    )


def test_replacing_the_body_of_a_file_with_no_frontmatter(library, write):
    write("plain", "Do the long thing.\n")
    library.replace_body(library.resolve("plain"), "Do it.")
    assert library.path_for("plain").read_text() == "Do it.\n"


def test_a_body_that_would_break_the_prompt_is_not_written(library, write):
    path = write("q", "---\nkind: ask\n---\nWhy {{thing}}?\n")
    with pytest.raises(PromptError, match="never closed"):
        library.replace_body(library.resolve("q"), "{{#thing}}Why?")
    assert path.read_text() == "---\nkind: ask\n---\nWhy {{thing}}?\n"
