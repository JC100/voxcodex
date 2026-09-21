"""Small reusable modal dialogs."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class PromptModal(ModalScreen[str]):
    """Asks for a single line of text (optionally masked) and returns it, or "" on cancel."""

    DEFAULT_CSS = """
    PromptModal {
        align: center middle;
    }
    PromptModal > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    PromptModal Static.message {
        margin-bottom: 1;
    }
    PromptModal Input {
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        title: str,
        message: str,
        password: bool = False,
        allow_empty: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._password = password
        self._allow_empty = allow_empty

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{self._title}[/b]")
            # markup=False: self._message can carry untrusted interpolated
            # text (e.g. a CAPTCHA URL from Amazon's login flow) -- Rich
            # markup parsing on that would raise MarkupError on a value
            # containing "[...]"-shaped text and crash the modal (L17).
            yield Static(self._message, classes="message", markup=False)
            yield Input(password=self._password, id="prompt-input")
            with Vertical():
                yield Button("Submit", variant="primary", id="submit")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#submit")
    def _submit_pressed(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss("")

    def _submit(self) -> None:
        value = self.query_one(Input).value
        if value or self._allow_empty:
            self.dismiss(value)


class ConfirmModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    """

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{self._title}[/b]")
            # markup=False: self._message can carry untrusted interpolated
            # text (a publisher-supplied book title, at least) -- Rich
            # markup parsing on that would raise MarkupError on a title
            # containing "[...]"-shaped text and crash the modal (L17).
            # Confirmed: common Audible suffixes like "[Unabridged]"
            # happen to survive only because of Rich's tag-character
            # rules, not by design.
            yield Static(self._message, markup=False)
            with Vertical():
                yield Button("Yes", variant="primary", id="yes")
                yield Button("No", id="no")

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)
