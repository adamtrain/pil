import pytest

from pil import config, shorten
from pil.shorten import Verdict
from pil.store import Prompt, parse
from pil.template import TemplateError


@pytest.mark.parametrize(
    ("before", "after", "enough"),
    [
        # Under 100 characters, 5% is less than 5 characters, so 5 is the bar.
        (40, 35, True),
        (40, 36, False),
        (100, 95, True),
        (100, 96, False),
        # Past that, it's 5%.
        (200, 190, True),
        (200, 191, False),
        (1000, 950, True),
        (1000, 951, False),
        (50, 50, False),
        (50, 60, False),
    ],
)
def test_a_rewrite_must_save_five_percent_or_five_characters_whichever_is_more(
    before, after, enough
):
    assert shorten.worthwhile("x" * before, "x" * after) is enough


def test_the_request_is_the_instruction_followed_by_the_prompts_text():
    prompt = parse("---\nkind: ask\nmodel: haiku\n---\nPlease explain {{thing}} in detail.", "p")
    invocation = shorten.invocation(prompt, config.Config())
    assert invocation.text == (
        "Maximally shorten this text, keeping information, grammar, and LLM-prompt usability."
        "\n\n<input>\nPlease explain {{thing}} in detail.\n</input>"
    )
    argv = invocation.argv
    # It runs like any transform, not with the settings of the prompt being shortened.
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--tools") + 1] == ""
    assert "{{…}} tag" in argv[argv.index("--system-prompt") + 1]


def test_tag_changes_reports_lost_and_new_variables():
    assert shorten.tag_changes("{{a}} {{#b}}x{{/b}}", "{{a}} {{#b}}x{{/b}}") == ([], [])
    assert shorten.tag_changes("{{a}} and {{b}}", "{{a}} {{c}}") == (["b"], ["c"])
    with pytest.raises(TemplateError):
        shorten.tag_changes("{{#a}}x{{/a}}", "{{#a}}x")


def test_each_pass_is_judged_against_the_text_before_it_and_the_original_variables():
    original = "x" * 180 + " {{ticket}}"
    shorter = "x" * 100 + " {{ticket}}"
    assert shorten.judge(original, original, shorter) == (Verdict.SHORTER, "")
    # Enough shorter than the original, but not than the pass before it.
    assert shorten.judge(original, shorter, shorter[3:]) == (Verdict.SPENT, "")
    verdict, reason = shorten.judge(original, shorter, "x" * 50)
    assert (verdict, reason) == (Verdict.BROKEN, "The shorter version dropped {{ticket}}.")
    verdict, reason = shorten.judge(original, shorter, "{{#ticket}}" + "x" * 50)
    assert verdict is Verdict.BROKEN
    assert "isn't a usable prompt" in reason
    verdict, reason = shorten.judge(original, shorter, "{{ticket}} {{who}} {{why}}")
    assert reason == "The shorter version added {{who}}, {{why}}."


def test_a_later_pass_shortens_the_text_it_is_given_not_the_prompts_own():
    prompt = parse("Long text.", "p")
    assert (
        "<input>\nShorter.\n</input>"
        in shorten.invocation(prompt, config.Config(), "Shorter.").text
    )


def test_a_spent_text_is_remembered_until_it_changes(library):
    library.save(Prompt(name="a", body="As short as it gets."))
    assert not shorten.is_spent("As short as it gets.")
    shorten.mark_spent("As short as it gets.", library)
    assert shorten.is_spent("As short as it gets.")
    assert shorten.is_spent("  As short as it gets.\n")
    assert not shorten.is_spent("As short as it gets. Or is it?")


def test_texts_no_prompt_has_any_more_are_forgotten(library, home):
    a = library.save(Prompt(name="a", body="First."))
    library.save(Prompt(name="b", body="Second."))
    shorten.mark_spent("First.", library)
    a.body = "First, rewritten."
    library.save(a)
    shorten.mark_spent("Second.", library)
    assert not shorten.is_spent("First.")
    assert shorten.is_spent("Second.")
    assert len((home / "state.json").read_text().splitlines()) == 5


def test_a_damaged_state_file_just_means_nothing_is_remembered(library, home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "state.json").write_text("not json")
    assert not shorten.is_spent("Anything.")
    library.save(Prompt(name="a", body="Anything."))
    shorten.mark_spent("Anything.", library)
    assert shorten.is_spent("Anything.")
