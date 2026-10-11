import pytest

from pil import fill
from pil.fill import FillError, parse_args
from pil.store import parse

PROMPT = parse(
    """\
---
vars:
  ticket: The ticket
  scope:
    options: [small, large]
    default: small
  tests:
    type: toggle
    default: true
---
{{ticket}} {{scope}} {{#tests}}t{{/tests}} {{#draft}}d{{/draft}} {{#notes}}{{notes}}{{/notes}}
""",
    "t",
)
VARS = PROMPT.variables()


def test_bare_words_fill_text_variables_required_first():
    assert parse_args(VARS, ["ABC-1"]) == {"ticket": "ABC-1"}
    assert parse_args(VARS, ["ABC-1", "mind", "the", "gap"]) == {
        "ticket": "ABC-1",
        "notes": "mind the gap",
    }


def test_leftover_words_join_the_last_variable_so_quotes_are_optional():
    one = parse("{{question}}", "q").variables()
    assert parse_args(one, ["why", "is", "it", "slow"]) == {"question": "why is it slow"}


def test_flags_and_assignments_name_a_variable():
    assert parse_args(VARS, ["--ticket", "ABC-1"]) == {"ticket": "ABC-1"}
    assert parse_args(VARS, ["--ticket=ABC-1"]) == {"ticket": "ABC-1"}
    assert parse_args(VARS, ["ticket=ABC-1", "scope=large"]) == {
        "ticket": "ABC-1",
        "scope": "large",
    }


def test_toggles_switch_on_and_off():
    assert parse_args(VARS, ["--draft", "--no-tests"]) == {"draft": True, "tests": False}
    assert parse_args(VARS, ["--tests=no", "draft=yes"]) == {"tests": False, "draft": True}


def test_choices_are_flags_and_accept_unambiguous_prefixes():
    assert parse_args(VARS, ["--scope", "large"]) == {"scope": "large"}
    assert parse_args(VARS, ["--scope=L"]) == {"scope": "large"}
    with pytest.raises(FillError, match="one of small, large"):
        parse_args(VARS, ["--scope=huge"])
    # Bare words never land in a choice.
    assert parse_args(VARS, ["ABC-1", "large"]) == {"ticket": "ABC-1", "notes": "large"}


def test_a_named_variable_no_longer_takes_bare_words():
    assert parse_args(VARS, ["--ticket=ABC-1", "look", "here"]) == {
        "ticket": "ABC-1",
        "notes": "look here",
    }


def test_words_that_only_look_like_assignments_are_just_words():
    one = parse("{{question}}", "q").variables()
    assert parse_args(one, ["is", "a=b", "valid?"]) == {"question": "is a=b valid?"}


def test_double_dash_makes_everything_after_it_text():
    one = parse("{{question}}", "q").variables()
    assert parse_args(one, ["--", "--force", "does", "what?"]) == {"question": "--force does what?"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--nope"], "no variable called 'nope'"),
        (["--ticket"], "--ticket needs a value"),
        (["--tests=maybe"], "yes or no"),
        (["--no-ticket"], "no variable called 'no-ticket'"),
    ],
)
def test_mistakes_are_explained(args, message):
    with pytest.raises(FillError, match=message):
        parse_args(VARS, args)


def test_words_with_nowhere_to_go_are_an_error():
    none = parse("Fixed text.", "f").variables()
    with pytest.raises(FillError, match="nowhere to put 'surprise words'"):
        parse_args(none, ["surprise", "words"])


def test_missing_and_defaults():
    assert [v.name for v in fill.missing(VARS, {})] == ["ticket"]
    assert fill.missing(VARS, {"ticket": "ABC-1"}) == []
    assert fill.with_defaults(VARS, {"ticket": "ABC-1", "tests": False}) == {
        "ticket": "ABC-1",
        "scope": "small",
        "tests": False,
        "draft": False,
        "notes": "",
    }


def test_synopsis_lists_words_then_flags():
    assert fill.synopsis(VARS) == "<ticket> [notes] [--scope=small|large] [--no-tests] [--draft]"


ASKED = parse(
    """\
---
vars:
  bug:
    options: {yes: resolve, no: implement}
  size:
    options: {s: Keep it small., l: Tidy as you go.}
---
{{bug}} {{size}}
""",
    "q",
).variables()


def test_a_question_is_answered_like_a_toggle():
    assert parse_args(ASKED, ["--bug"]) == {"bug": True}
    assert parse_args(ASKED, ["--no-bug"]) == {"bug": False}
    assert parse_args(ASKED, ["bug=y", "--size", "l"]) == {"bug": True, "size": "l"}
    assert [v.name for v in fill.missing(ASKED, {})] == ["bug", "size"]
    assert fill.synopsis(ASKED) == "--bug|--no-bug --size=s|l"


def test_the_text_an_answer_stands_for_is_accepted_as_the_answer():
    assert parse_args(ASKED, ["--bug=implement"]) == {"bug": False}
    assert parse_args(ASKED, ["--size=Tidy as you go."]) == {"size": "l"}
    with pytest.raises(FillError, match="yes or no"):
        parse_args(ASKED, ["--bug=maybe"])
