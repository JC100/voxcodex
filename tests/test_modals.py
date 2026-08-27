from textual.app import App
from textual.widgets import Input

from audible_tui.screens.modals import ConfirmModal, MessageModal, PromptModal


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


# -- MessageModal -------------------------------------------------------


async def test_message_modal_ok_dismisses_with_none():
    results = []
    modal = MessageModal("Title", "Something happened")
    app = ModalHostApp(modal, results.append)

    async with app.run_test() as pilot:
        await pilot.click("#ok")
        await pilot.pause()

    assert results == [None]


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
