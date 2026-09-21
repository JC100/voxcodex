from textual.app import App
from textual.widgets import Input, Static

from voxcodex.screens.modals import ConfirmModal, PromptModal


class ModalHostApp(App):
    def __init__(self, modal, callback):
        super().__init__()
        self._modal = modal
        self._callback = callback

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._callback)


# -- PromptModal ------------------------------------------------------------


async def test_prompt_modal_submits_typed_value():
    results = []
    modal = PromptModal("Title", "Message")
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.press(*"hello")
        await pilot.click("#submit")
        await pilot.pause()

    assert results == ["hello"]


async def test_prompt_modal_enter_key_submits():
    results = []
    modal = PromptModal("Title", "Message")
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.press(*"world")
        await pilot.press("enter")
        await pilot.pause()

    assert results == ["world"]


async def test_prompt_modal_cancel_returns_empty_string():
    results = []
    modal = PromptModal("Title", "Message")
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.press(*"typed but cancelled")
        await pilot.click("#cancel")
        await pilot.pause()

    assert results == [""]


async def test_prompt_modal_rejects_empty_submit_by_default():
    """Submitting blank should not dismiss the modal at all when allow_empty
    is False -- the caller is still waiting on a real answer."""
    results = []
    modal = PromptModal("Title", "Message", allow_empty=False)
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.click("#submit")
        await pilot.pause()

        assert results == []  # never dismissed
        assert modal.query_one(Input).value == ""


async def test_prompt_modal_allows_empty_submit_when_configured():
    results = []
    modal = PromptModal("Title", "Message", allow_empty=True)
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.click("#submit")
        await pilot.pause()

    assert results == [""]


async def test_prompt_modal_message_with_markup_shaped_text_does_not_raise():
    """L17: the message is rendered as Rich markup by default -- a
    publisher-supplied book title, or an Amazon-provided CAPTCHA URL,
    could contain "[...]"-shaped text and raise MarkupError, crashing the
    modal. Confirmed: common Audible suffixes like "[Unabridged]" happen
    to survive only because of Rich's tag-character rules (an unrecognized
    tag name is tolerated); an unmatched *closing* tag like "[/foo]" is
    what actually raises, and is the shape this test uses."""
    modal = PromptModal("Title", "Open this URL: http://x/[/not_a_real_tag]")
    app = ModalHostApp(modal, lambda result: None)

    async with app.run_test():  # must not raise
        assert "[/not_a_real_tag]" in str(modal.query_one(".message", Static).content)


# -- ConfirmModal -------------------------------------------------------


async def test_confirm_modal_yes_returns_true():
    results = []
    modal = ConfirmModal("Delete?", "Are you sure?")
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.click("#yes")
        await pilot.pause()

    assert results == [True]


async def test_confirm_modal_no_returns_false():
    results = []
    modal = ConfirmModal("Delete?", "Are you sure?")
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.click("#no")
        await pilot.pause()

    assert results == [False]


async def test_confirm_modal_message_with_markup_shaped_title_does_not_raise():
    """L17: at least one real call site interpolates a publisher-supplied
    book title into this message (library.py's delete-download confirm)
    -- a title containing an unmatched closing-tag-shaped substring like
    "[/something]" raised MarkupError inside the confirm dialog."""
    title = "Some Book [/vol_two] Extended Edition"
    modal = ConfirmModal("Delete download", f"Delete the local copy of '{title}'?")
    app = ModalHostApp(modal, lambda result: None)

    async with app.run_test():  # must not raise
        statics = modal.query(Static)
        assert title in str(statics[1].content)
