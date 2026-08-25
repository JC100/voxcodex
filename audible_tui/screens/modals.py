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
            yield Static(self._message, classes="message")
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


class MessageModal(ModalScreen[None]):
    """Shows a message with an OK button."""

    DEFAULT_CSS = """
    MessageModal {
        align: center middle;
    }
    MessageModal > Vertical {
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
            yield Static(self._message)
            yield Button("OK", variant="primary", id="ok")

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(None)


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
            yield Static(self._message)
            with Vertical():
                yield Button("Yes", variant="primary", id="yes")
                yield Button("No", id="no")

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)
