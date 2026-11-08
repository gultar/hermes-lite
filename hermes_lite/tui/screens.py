"""Modal screens: command approval, clarify questions, session picker."""

from __future__ import annotations

import datetime as dt
from typing import Optional

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

    def __init__(self, prompt: str, command: str, placeholder: str = "", remember: Optional[str] = None,
                 send: str = ""):
        super().__init__()
        self.prompt, self.command = prompt, command
        self.placeholder = placeholder or "Your password (hidden; goes to sudo only, never to the model)"
        self.remember = "Remember until Hermes Lite quits (kept in memory only)" if remember is None else remember
        self.send = send or "Send to sudo"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog secret"):
            yield Label(self.prompt or "Password:", classes="dialog-title", markup=False)
            if self.command:
                yield Static(self.command, classes="dialog-code", markup=False)
            yield Input(password=True, placeholder=self.placeholder, id="secret")
            yield Checkbox(self.remember, id="remember", classes="" if self.remember else "hidden")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.send, id="ok", variant="primary")
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


class SkillBrowser(ModalScreen[list]):
    """/skills browse: search the catalogue, read a skill, Enter adds it (or removes one you added)."""
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, entries: list[dict], toggle):
        super().__init__()
        self.entries, self.toggle, self.changes = entries, toggle, []
        self.shown: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog skillbrowser"):
            yield Label(f"Skills catalogue · {len(self.entries)} skills · Enter adds / removes · Esc closes",
                        classes="dialog-title")
            yield Input(placeholder="Search: name, category or words from the description", id="skill-search")
            with Horizontal(classes="skillbrowser-body"):
                yield OptionList(id="skill-list")
                with VerticalScroll(id="skill-preview-scroll"):
                    yield Static("", id="skill-preview", markup=False)
            yield Static("", id="skill-status", classes="dialog-keys", markup=False)

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#skill-search", Input).focus()

    def _label(self, e: dict) -> str:
        mark = "✓" if e["added"] else "⚠" if e["hints"] else " "
        return f"{mark} {e['name'][:34]:<34} {e['category'][:22]}"

    def _fill(self, query: str) -> None:
        from ..skillcatalog import matches
        self.shown = [e for e in self.entries if matches(e, query)]
        ol = self.query_one("#skill-list", OptionList)
        ol.clear_options()
        ol.add_options([Option(self._label(e), id=str(i)) for i, e in enumerate(self.shown)])
        if self.shown:
            ol.highlighted = 0
            self._preview(self.shown[0])
        else:
            self.query_one("#skill-preview", Static).update("No skill matches.")
        self.query_one("#skill-status", Static).update(
            f"{len(self.shown)} shown · ✓ added · ⚠ may need tools Hermes Lite lacks · Tab: list ↔ search")

    def _preview(self, e: dict) -> None:
        lines = [e["name"], e["description"], f"{e['category']} · {e['dir']}"]
        if e["hints"]:
            lines.append("⚠ " + "; ".join(e["hints"]))
        lines.append("✓ added (Enter removes it)" if e["removable"] else "✓ already in your skills" if e["added"]
                     else "Enter adds it to ~/.hermes-lite/skills")
        body = e["body"].strip().splitlines()
        lines += ["", *body[:80], *(["", f"… {len(body) - 80} more lines"] if len(body) > 80 else [])]
        self.query_one("#skill-preview", Static).update("\n".join(lines))
        self.query_one("#skill-preview-scroll", VerticalScroll).scroll_home(animate=False)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#skill-list", OptionList).focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self._preview(self.shown[int(event.option.id)])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        e = self.shown[int(event.option.id)]
        message = self.toggle(e)          # adds or removes; returns what happened
        self.changes.append(message)
        self.query_one("#skill-list", OptionList).replace_option_prompt(event.option.id, self._label(e))
        self._preview(e)
        self.query_one("#skill-status", Static).update(message)

    def action_close(self) -> None:
        self.dismiss(self.changes)
