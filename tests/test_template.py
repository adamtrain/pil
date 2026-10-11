import pytest

from pil.template import Answer, TemplateError, render, variables


def test_variables_are_substituted():
    assert render("Fix {{ticket}} on {{ branch }}.", {"ticket": "ABC-1", "branch": "main"}) == (
        "Fix ABC-1 on main."
    )


def test_sections_follow_their_variable():
    text = "Go.{{#tests}} Write tests.{{/tests}}{{^tests}} Skip tests.{{/tests}}"
    assert render(text, {"tests": True}) == "Go. Write tests."
    assert render(text, {"tests": False}) == "Go. Skip tests."


def test_a_section_is_on_when_its_text_is_not_empty():
    text = "{{#focus}}Look at {{focus}}.{{/focus}}"
    assert render(text, {"focus": "auth"}) == "Look at auth."
    assert render(text, {"focus": ""}) == ""


def test_section_tags_on_their_own_line_leave_no_blank_lines():
    text = "One.\n{{#more}}\nTwo.\n{{/more}}\nThree."
    assert render(text, {"more": True}) == "One.\nTwo.\nThree."
    assert render(text, {"more": False}) == "One.\nThree."


def test_sections_nest():
    text = "{{#a}}a{{#b}}b{{/b}}{{/a}}"
    assert render(text, {"a": True, "b": True}) == "ab"
    assert render(text, {"a": True, "b": False}) == "a"
    assert render(text, {"a": False}) == ""


def test_other_double_braces_are_left_alone():
    text = "token: ${{ secrets.TOKEN }} and {{ 1 + 1 }} and {{}}"
    assert render(text, {}) == text
    assert variables(text) == {}


def test_a_backslash_makes_a_tag_literal():
    assert render(r"Write \{{name}} for a variable.", {}) == "Write {{name}} for a variable."
    assert variables(r"\{{name}}") == {}


def test_values_are_not_scanned_for_tags():
    assert render("{{input}}", {"input": "{{secret}} {{#x}}"}) == "{{secret}} {{#x}}"


def test_toggles_read_as_words_when_substituted():
    assert render("tests: {{tests}}", {"tests": True}) == "tests: yes"


def test_variables_reports_how_each_name_is_used():
    text = "{{ticket}} {{#tests}}t{{/tests}} {{#focus}}{{focus}}{{/focus}} {{ticket}}"
    assert variables(text) == {"ticket": "text", "tests": "toggle", "focus": "optional"}


def test_only_names_in_use_are_looked_up():
    class Lazy(dict):
        def __missing__(self, key):
            raise AssertionError(f"looked up {key}")

    assert render("{{#on}}{{expensive}}{{/on}}done", Lazy(on=False)) == "done"


@pytest.mark.parametrize(
    ("text", "message", "line"),
    [
        ("a\n{{#open}}\nb", "never closed", 2),
        ("a\nb {{/shut}}", "nothing is open", 2),
        ("{{#a}}\n{{#b}}\n{{/a}}", "{{#b}} is still open", 3),
    ],
)
def test_unbalanced_sections_are_reported_with_a_line(text, message, line):
    with pytest.raises(TemplateError) as e:
        render(text, {"a": True, "b": True, "open": True})
    assert message in str(e.value)
    assert e.value.line == line


def test_a_missing_value_is_an_error():
    with pytest.raises(TemplateError, match=r"no value for \{\{ticket\}\}"):
        render("{{ticket}}", {})


def test_an_answer_reads_as_its_text_but_switches_sections_by_its_yes_or_no():
    text = "{{bug}}{{#bug}} (bug){{/bug}}{{^bug}} (feature){{/bug}}"
    assert render(text, {"bug": Answer(True, "resolve")}) == "resolve (bug)"
    assert render(text, {"bug": Answer(False, "implement")}) == "implement (feature)"
    assert render(text, {"bug": Answer(True, "")}) == " (bug)"
