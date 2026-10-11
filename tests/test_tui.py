import pytest
from rich.console import Console
from textual.widgets import Checkbox, Input, Select, Static, TextArea

from pil import config
from pil.tui.app import Browser, PilApp, PromptList
from pil.tui.dialogs import AskName, Confirm
from pil.tui.editor import Editor, VariableCard
from pil.tui.run import FillForm, ResultScreen
from pil.tui.shorten import ShortenScreen


@pytest.fixture
def prompts(write):
    write(
        "work/ticket",
        """\
        ---
        description: Start work on a ticket
        mode: plan
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
        Write tests first.
        {{/tests}}
        """,
    )
    write("text/fix", "---\nkind: transform\ndescription: Fix typos\n---\nFix it.")
    write("copy/sig", "---\nkind: copy\n---\nRegards, Adam")
    write("broken", "---\nkind: telepathy\n---\nHm.")


@pytest.fixture
def app(library, clipboard):
    return PilApp(library, config.load())


def names(app) -> list[str]:
    return [option.id for option in app.screen.query_one(PromptList).options if option.id]


async def select(pilot, name: str) -> None:
    listing = pilot.app.screen.query_one(PromptList)
    listing.highlighted = [option.id for option in listing.options].index(name)
    await pilot.pause()


def shown(widget: Static) -> str:
    """The text a Static is displaying."""
    console = Console(width=200, color_system=None)
    with console.capture() as capture:
        console.print(widget.content)
    return capture.get()


async def test_the_browser_lists_prompts_by_folder_and_previews_the_first(app, prompts):
    async with app.run_test(size=(120, 40)) as pilot:
        assert names(app) == ["broken", "copy/sig", "text/fix", "work/ticket"]
        assert app.screen.selected.name == "broken"
        await select(pilot, "work/ticket")
        details = app.screen.query_one("#details", Static)
        assert "Start work on a ticket" in shown(details)


async def test_an_empty_library_says_how_to_begin(app):
    async with app.run_test(size=(120, 40)):
        assert "No prompts yet" in shown(app.screen.query_one("#details", Static))
        assert app.screen.selected is None


async def test_filtering_narrows_the_list_and_escape_brings_it_back(app, prompts):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("slash", *"fix")
        assert names(app) == ["text/fix"]
        assert app.screen.selected.name == "text/fix"
        await pilot.press("escape")
        assert len(names(app)) == 4
        assert isinstance(app.focused, PromptList)
        await pilot.press("slash", *"zzz")
        assert names(app) == []
        assert "Nothing matches" in shown(app.screen.query_one("#details", Static))


async def test_arrow_keys_work_from_the_filter_box(app, prompts):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("slash", "down", "down")
        assert app.screen.selected.name == "text/fix"
        assert isinstance(app.focused, Input)


async def test_a_copy_prompt_lands_on_the_clipboard(app, prompts, clipboard):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "copy/sig")
        await pilot.press("enter")
        assert clipboard["text"] == "Regards, Adam"
        assert isinstance(app.screen, Browser)


async def test_a_session_prompt_is_filled_in_then_closes_the_app_with_its_command(app, prompts):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "work/ticket")
        await pilot.press("enter")
        form = app.screen
        assert isinstance(form, FillForm)
        # Nothing typed yet, so Enter stays put rather than running half-filled.
        await pilot.press("enter")
        assert app.screen is form
        await pilot.press(*"ABC-1")
        assert "Work on ABC-1 (small)." in shown(form.query_one("#filled", Static))
        form.query_one("#var-tests", Checkbox).value = False
        form.query_one("#var-scope", Select).value = "large"
        await pilot.press("ctrl+r")
    assert app.return_value == [
        "claude",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "high",
        "--permission-mode",
        "plan",
        "--",
        "Work on ABC-1 (large).",
    ]


async def test_escape_leaves_the_form_without_running(app, prompts):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "work/ticket")
        await pilot.press("enter", "escape")
        assert isinstance(app.screen, Browser)
    assert app.return_value is None


async def test_a_transform_shows_its_result_and_can_be_undone(app, prompts, clipboard, fake_claude):
    fake_claude(result="The text")
    clipboard["text"] = "teh text"
    async with PilApp(app.library, config.load()).run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("enter")
        result = pilot.app.screen
        assert isinstance(result, ResultScreen)
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert clipboard["text"] == "The text"
        assert "clipboard updated" in shown(result.query_one("#status", Static))
        await pilot.press("u")
        assert clipboard["text"] == "teh text"
        await pilot.press("escape")
        assert isinstance(pilot.app.screen, Browser)


async def test_a_failed_run_explains_itself_and_spares_the_clipboard(
    app, prompts, clipboard, fake_claude
):
    fake_claude(is_error=True, result="Overloaded.")
    clipboard["text"] = "precious"
    async with PilApp(app.library, config.load()).run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("enter")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert "Overloaded." in shown(pilot.app.screen.query_one("#result Static", Static))
        assert clipboard["text"] == "precious"


async def test_an_empty_clipboard_and_a_broken_prompt_are_not_run(app, prompts, clipboard):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("enter")
        assert isinstance(app.screen, Browser)
        await select(pilot, "broken")
        await pilot.press("enter")
        assert isinstance(app.screen, Browser)


async def test_writing_a_new_prompt(app, prompts, library):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("n")
        editor = app.screen
        assert isinstance(editor, Editor)
        # It starts in the folder you were looking at.
        await pilot.press(*"greet")
        assert editor.query_one("#name", Input).value == "text/greet"
        editor.query_one("#kind", Select).value = "copy"
        editor.query_one("#body", TextArea).text = "Hi {{who}}{{#loud}}!{{/loud}}"
        await pilot.pause()
        cards = {card.variable.name: card for card in editor.query(VariableCard)}
        assert {name: card.type for name, card in cards.items()} == {
            "who": "text",
            "loud": "toggle",
        }
        cards["who"].query_one(".card-help", Input).value = "Who to greet"
        cards["who"].query_one(".card-default", Input).value = "world"
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, Browser)
        assert app.screen.selected.name == "text/greet"
    saved = library.resolve("greet")
    assert saved.kind.name == "copy"
    assert saved.vars == {"who": {"default": "world", "help": "Who to greet"}}
    assert [(v.name, v.type, v.default) for v in saved.variables()] == [
        ("who", "text", "world"),
        ("loud", "toggle", False),
    ]


async def test_editing_keeps_what_the_form_does_not_show(app, prompts, library):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "work/ticket")
        await pilot.press("e")
        editor = app.screen
        editor.query_one("#about", Input).value = "Begin a ticket"
        editor.query_one("#tags", Input).value = "work, #jira"
        await pilot.press("ctrl+s")
    saved = library.resolve("ticket")
    assert saved.description == "Begin a ticket"
    assert saved.tags == ["work", "jira"]
    assert saved.options == {"mode": "plan"}
    assert saved.vars == {
        "ticket": "The ticket number",
        "tests": {"default": True},
        "scope": {"options": ["small", "large"], "default": "small"},
    }
    assert saved.body.startswith("Work on {{ticket}} ({{scope}}).")


async def test_renaming_in_the_editor_moves_the_file(app, prompts, library):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("e")
        app.screen.query_one("#name", Input).value = "writing/typos"
        await pilot.press("ctrl+s")
    assert "writing/typos" in library.names()
    assert "text/fix" not in library.names()


async def test_a_bad_save_stays_in_the_editor(app, prompts, library):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("e")
        app.screen.query_one("#name", Input).value = "copy/sig"
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, Editor)
    assert library.resolve("text/fix").body == "Fix it."


async def test_leaving_the_editor_with_changes_asks_first(app, prompts, library):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("e", "escape")
        assert isinstance(app.screen, Browser)
        await pilot.press("e")
        editor = app.screen
        editor.query_one("#body", TextArea).text = "Changed."
        await pilot.pause()
        await pilot.press("escape")
        assert isinstance(app.screen, Confirm)
        await pilot.press("n")
        assert app.screen is editor
        await pilot.press("escape", "y")
        assert isinstance(app.screen, Browser)
    assert library.resolve("text/fix").body == "Fix it."


async def test_rename_duplicate_and_delete(app, prompts, library):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("m")
        assert isinstance(app.screen, AskName)
        app.screen.query_one(Input).value = "writing/fix"
        await pilot.press("enter")
        assert app.screen.selected.name == "writing/fix"

        await pilot.press("c", "enter")
        assert app.screen.selected.name == "writing/fix-copy"

        await pilot.press("d")
        assert isinstance(app.screen, Confirm)
        await pilot.press("escape")
        assert "writing/fix-copy" in library.names()
        await pilot.press("d", "y")
    assert library.names() == ["broken", "copy/sig", "work/ticket", "writing/fix"]


async def test_copying_a_prompts_text(app, prompts, clipboard):
    async with app.run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("y")
        assert clipboard["text"] == "Fix it."


async def test_backing_out_of_a_slow_run_cancels_it(app, prompts, clipboard, fake_claude):
    fake_claude.path.write_text("#!/bin/sh\nsleep 30\n")
    clipboard["text"] = "precious"
    async with PilApp(app.library, config.load()).run_test(size=(120, 40)) as pilot:
        await select(pilot, "text/fix")
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, ResultScreen)
        await pilot.press("escape")
        await pilot.app.workers.wait_for_complete()
        assert isinstance(pilot.app.screen, Browser)
    assert clipboard["text"] == "precious"


async def test_prompts_changed_on_disk_show_up_after_a_reload(app, prompts, write):
    async with app.run_test(size=(120, 40)) as pilot:
        write("zz-new", "Fresh.")
        await pilot.press("r")
        assert "zz-new" in names(app)


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
          size:
            options:
              s: Keep it small.
              l: Tidy up, as you go.
        ---
        Please {{bug}} {{ticket}}. {{size}}
        """,
    )


async def test_the_form_insists_on_an_answer_to_a_question(app, question):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("enter")
        form = app.screen
        form.query_one("#var-ticket", Input).value = "ABC-1"
        await pilot.press("ctrl+r")
        # Still unanswered, so it hasn't run.
        assert app.screen is form
        form.query_one("#var-bug", Select).value = False
        form.query_one("#var-size", Select).value = "s"
        await pilot.pause()
        assert "Please implement ABC-1. Keep it small." in shown(form.query_one("#filled", Static))
        await pilot.press("ctrl+r")
    assert app.return_value[-1] == "Please implement ABC-1. Keep it small."


async def test_the_editor_keeps_answer_texts_and_lets_them_be_changed(app, question, library):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("e")
        cards = {card.variable.name: card for card in app.screen.query(VariableCard)}
        assert cards["bug"].query_one(".card-yes", Input).value == "resolve"
        assert cards["bug"].query_one(".card-unset", Select).value == "ask"
        cards["bug"].query_one(".card-no", Input).value = "build"
        # A text with a comma in it can't be edited on one line, so it's left alone.
        assert cards["size"].query_one(".card-options", Input).disabled
        await pilot.press("ctrl+s")
    saved = {variable.name: variable for variable in library.resolve("triage").variables()}
    assert saved["bug"].texts == (("yes", "resolve"), ("no", "build"))
    assert saved["bug"].required
    assert saved["size"].texts == (("s", "Keep it small."), ("l", "Tidy up, as you go."))
    assert "yes: resolve" in library.path_for("triage").read_text()


async def test_the_editor_can_give_a_toggle_text_for_no_only(app, library, write):
    write("plan", "Plan it.\n{{tests}}")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("e")
        card = app.screen.query_one(VariableCard)
        card.query_one(".card-type", Select).value = "toggle"
        card.query_one(".card-no", Input).value = "Skip the tests."
        card.query_one(".card-unset", Select).value = "yes"
        await pilot.press("ctrl+s")
    (tests,) = library.resolve("plan").variables()
    assert (tests.type, tests.default) == ("toggle", True)
    assert tests.texts == (("yes", ""), ("no", "Skip the tests."))


async def test_the_editor_reads_aliased_options_from_one_line(app, library, write):
    write("go", "{{how}}")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("e")
        card = app.screen.query_one(VariableCard)
        card.query_one(".card-type", Select).value = "choice"
        card.query_one(".card-options", Input).value = "fast=Be quick., careful"
        await pilot.press("ctrl+s")
    (how,) = library.resolve("go").variables()
    assert how.options == ("fast", "careful")
    assert how.resolve("fast") == "Be quick."
    assert how.resolve("careful") == "careful"


# ── Shortening ────────────────────────────────────────────────────────────────

WORDY = "Please would you kindly work on the ticket {{ticket}} for me, with great care."
SHORTER = "Please work on the ticket {{ticket}}, with care."
SHORTEST = "Carefully work on {{ticket}}."
FRONT = "---\nkind: session\n# keep me\nmode: plan\n---\n\n"


@pytest.fixture
def wordy(write):
    return write("wordy", f"{FRONT}{WORDY}\n")


def notices(app) -> list[str]:
    return [notification.message for notification in app._notifications]


def texts_sent(fake_claude) -> list[str]:
    """The text each call to Claude was asked to shorten."""
    return [
        call["prompt"].partition("<input>\n")[2].removesuffix("\n</input>")
        for call in fake_claude.calls()
    ]


async def shorten(pilot) -> None:
    await pilot.press("s")
    # Each pass starts the next, so wait until none is left running.
    for _ in range(50):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        if all(worker.is_finished for worker in pilot.app.workers):
            break


def browsing(library):
    return PilApp(library, config.load()).run_test(size=(120, 40))


async def test_shortening_repeats_until_a_pass_stops_paying_then_offers_the_last_good_one(
    library, clipboard, fake_claude, wordy
):
    # The third answer is only one character shorter, so the second is as good as it gets.
    fake_claude(results=[SHORTER, SHORTEST, SHORTEST[:-2] + "."])
    async with browsing(library) as pilot:
        await shorten(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ShortenScreen)
        before, after = (shown(pane) for pane in screen.query(".pane"))
        assert WORDY in before
        assert SHORTEST in after
        status = shown(screen.query_one("#status", Static))
        assert "63% shorter" in status
        assert "2 passes" in status
        # Nothing is written until it's approved.
        assert wordy.read_text() == f"{FRONT}{WORDY}\n"
        await pilot.press("y")
        assert isinstance(pilot.app.screen, Browser)
        assert pilot.app.screen.selected.body == SHORTEST
    assert wordy.read_text() == f"{FRONT}{SHORTEST}\n"
    # Each pass was given the one before it.
    assert texts_sent(fake_claude) == [WORDY, SHORTER, SHORTEST]
    assert fake_claude.calls()[0]["prompt"].startswith(
        "Maximally shorten this text, keeping information, grammar, and LLM-prompt usability."
    )


async def test_once_shortened_asking_again_does_not_bother_claude(
    library, clipboard, fake_claude, wordy
):
    fake_claude(results=[SHORTEST, SHORTEST])
    async with browsing(library) as pilot:
        await shorten(pilot)
        await pilot.press("y")
        assert len(fake_claude.calls()) == 2
        await shorten(pilot)
        assert isinstance(pilot.app.screen, Browser)
        assert notices(pilot.app)[-1] == "It can't get any shorter."
        assert len(fake_claude.calls()) == 2


async def test_editing_a_shortened_prompt_lets_it_be_shortened_again(
    library, clipboard, fake_claude, wordy
):
    fake_claude(results=[SHORTEST, SHORTEST])
    async with browsing(library) as pilot:
        await shorten(pilot)
        await pilot.press("y", "e")
        pilot.app.screen.query_one("#body", TextArea).text = WORDY
        await pilot.pause()
        await pilot.press("ctrl+s")
        await shorten(pilot)
        assert isinstance(pilot.app.screen, ShortenScreen)
    assert texts_sent(fake_claude)[2] == WORDY


async def test_a_change_made_outside_pil_also_lets_it_be_shortened_again(
    library, clipboard, fake_claude, wordy
):
    fake_claude(results=[SHORTEST, SHORTEST])
    async with browsing(library) as pilot:
        await shorten(pilot)
        await pilot.press("y")
        wordy.write_text(f"{FRONT}{SHORTEST} Mind the tests too.\n")
        await pilot.press("r")
        await shorten(pilot)
    assert texts_sent(fake_claude)[2] == f"{SHORTEST} Mind the tests too."


async def test_a_prompt_that_cannot_get_shorter_says_so_and_is_remembered(
    library, clipboard, fake_claude, wordy
):
    # Four characters shorter, where five are needed.
    fake_claude(result=WORDY.replace("kindly", "do"))
    async with browsing(library) as pilot:
        await shorten(pilot)
        assert isinstance(pilot.app.screen, Browser)
        assert notices(pilot.app) == ["It can't get any shorter."]
        await shorten(pilot)
        assert notices(pilot.app) == ["It can't get any shorter."] * 2
    assert wordy.read_text() == f"{FRONT}{WORDY}\n"
    assert len(fake_claude.calls()) == 1


async def test_turning_the_offer_down_changes_and_remembers_nothing(
    library, clipboard, fake_claude, wordy
):
    fake_claude(results=[SHORTEST, SHORTEST])
    async with browsing(library) as pilot:
        await shorten(pilot)
        await pilot.press("n")
        assert isinstance(pilot.app.screen, Browser)
        await shorten(pilot)
        assert isinstance(pilot.app.screen, ShortenScreen)
    assert wordy.read_text() == f"{FRONT}{WORDY}\n"
    assert len(fake_claude.calls()) == 4


async def test_a_pass_that_loses_a_variable_is_thrown_away(library, clipboard, fake_claude, wordy):
    fake_claude(results=[SHORTER, "Carefully work on it."])
    async with browsing(library) as pilot:
        await shorten(pilot)
        screen = pilot.app.screen
        assert SHORTER in shown(list(screen.query(".pane"))[1])
        assert "dropped {{ticket}}" in shown(screen.query_one("#note", Static))
        await pilot.press("y")
        # That wasn't the end of the road, so asking again does try again.
        await shorten(pilot)
    assert texts_sent(fake_claude) == [WORDY, SHORTER, SHORTER]


async def test_a_first_pass_that_breaks_the_prompt_offers_nothing(
    library, clipboard, fake_claude, wordy
):
    fake_claude(result="{{#ticket}}Work on it.")
    async with browsing(library) as pilot:
        await shorten(pilot)
        screen = pilot.app.screen
        assert "can't be used" in shown(screen.query_one("#result Static", Static))
        await pilot.press("y")
        assert pilot.app.screen is screen
    assert wordy.read_text() == f"{FRONT}{WORDY}\n"


async def test_backing_out_part_way_through_stops_the_passes(
    library, clipboard, fake_claude, wordy
):
    fake_claude.path.write_text("#!/bin/sh\nsleep 30\n")
    async with browsing(library) as pilot:
        await pilot.press("s")
        assert isinstance(pilot.app.screen, ShortenScreen)
        await pilot.press("escape")
        await pilot.app.workers.wait_for_complete()
        assert isinstance(pilot.app.screen, Browser)
    assert wordy.read_text() == f"{FRONT}{WORDY}\n"


async def test_broken_and_empty_prompts_are_not_sent_for_shortening(app, prompts, write):
    write("empty", "---\nkind: ask\n---\n")
    async with app.run_test(size=(120, 40)) as pilot:
        for name in ("broken", "empty"):
            await select(pilot, name)
            await pilot.press("s")
            assert isinstance(app.screen, Browser)
