"""Modal screens: command approval, clarify questions, session picker."""

from __future__ import annotations

import datetime as dt

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option


class ApprovalScreen(ModalScreen[str]):
    BINDINGS = [Binding("y", "pick('once')", "Allow once"), Binding("a", "pick('session')", "Allow for session"),
                Binding("n,escape", "pick('deny')", "Deny")]

    def __init__(self, command: str, reason: str):
        super().__init__()
        self.command, self.reason = command, reason

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog approval"):
            yield Label(self.reason_phrase(), classes="dialog-title", markup=False)
            # A long command scrolls inside its box; the buttons below always stay on screen.
            with VerticalScroll(classes="dialog-code-scroll"):
                yield Static(self.command, classes="dialog-code", markup=False)
            yield Label("y allow once · a allow for this session · n or Esc deny · /yolo stops these questions",
                        classes="dialog-keys", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Allow once  y", id="once", variant="primary")
                yield Button("Allow for this session  a", id="session")
                yield Button("Deny  n", id="deny", variant="error")

    def reason_phrase(self) -> str:
        if self.reason.startswith("plan mode"):
            return "Plan mode: this command isn't on the read-only list. Run it anyway?"
        return f"Run this command? It matches a risky pattern: {self.reason}."

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "deny")

    def action_pick(self, verdict: str) -> None:
        self.dismiss(verdict)


class ClarifyScreen(ModalScreen[list]):
    BINDINGS = [Binding("escape", "cancel", "Dismiss")]

    def __init__(self, questions: list[dict]):
        super().__init__()
        self.questions = questions

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog clarify"):
            yield Label("Hermes needs a decision", classes="dialog-title")
            with VerticalScroll(classes="clarify-scroll"):
                for i, q in enumerate(self.questions):
                    yield Label(q["question"], classes="clarify-q", markup=False)
                    choices = q.get("choices") or []
                    if choices and q.get("multi_select"):
                        yield SelectionList[str](*[(c, c) for c in choices], id=f"q{i}")
                    elif choices:
                        opts = [Option(("★ " if j == 0 else "  ") + c, id=f"c{j}") for j, c in enumerate(choices)]
                        yield OptionList(*opts, id=f"q{i}")
                    yield Input(placeholder="Other / your answer", id=f"t{i}")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Send answers", id="send", variant="primary")
                yield Button("Let Hermes decide", id="skip")

    def _answers(self) -> list:
        out = []
        for i, q in enumerate(self.questions):
            typed = self.query_one(f"#t{i}", Input).value.strip()
            if typed:
                out.append(typed)
                continue
            choices = q.get("choices") or []
            if choices and q.get("multi_select"):
                out.append(list(self.query_one(f"#q{i}", SelectionList).selected))
            elif choices:
                ol = self.query_one(f"#q{i}", OptionList)
                out.append(choices[ol.highlighted] if ol.highlighted is not None else "")
            else:
                out.append("")
        return out

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._answers() if event.button.id == "send" else [])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(self._answers())

    def action_cancel(self) -> None:
        self.dismiss([])


class SessionScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, sessions: list[dict]):
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog sessions"):
            yield Label("Resume a session", classes="dialog-title")
            if not self.sessions:
                yield Static("No saved sessions yet.", classes="muted")
            opts = []
            for s in self.sessions:
                when = dt.datetime.fromtimestamp(s["updated"]).strftime("%b %d %H:%M")
                opts.append(Option(f"{s['title'][:58]:<58}  {s['turns']:>3} msgs  {when}", id=s["id"]))
            yield OptionList(*opts, id="session-list")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id or "")

    def action_cancel(self) -> None:
        self.dismiss("")


class SecretScreen(ModalScreen[tuple]):
    """Masked password box for sudo. The answer goes to sudo only, never to the model."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, command: str):
        super().__init__()
        self.prompt, self.command = prompt, command

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog secret"):
            yield Label(self.prompt or "Password:", classes="dialog-title", markup=False)
            if self.command:
                yield Static(self.command, classes="dialog-code", markup=False)
            yield Input(password=True, placeholder="Your password (hidden; goes to sudo only, never to the model)",
                        id="secret")
            yield Checkbox("Remember until Hermes Lite quits (kept in memory only)", id="remember")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Send to sudo", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#secret", Input).focus()

    def _answer(self) -> tuple:
        return self.query_one("#secret", Input).value, self.query_one("#remember", Checkbox).value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(self._answer())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._answer() if event.button.id == "ok" else (None, False))

    def action_cancel(self) -> None:
        self.dismiss((None, False))
