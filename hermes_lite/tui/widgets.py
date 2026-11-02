"""Chat-log widgets: user message, thinking block, assistant markdown, tool card, header, sidebar, input."""

from __future__ import annotations

import json
import re
import time

from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Collapsible, Markdown, Static, TextArea

BRONZE, VERDIGRIS, CINNABAR, DUSK, PARCHMENT, MUTED = "#c8a04a", "#6fb5a0", "#d0705f", "#8c90b8", "#e7e1d1", "#7d8399"


class UserMessage(Static):
    def __init__(self, text: str, kind: str = "user"):
        super().__init__(text, markup=False)
        self.add_class(kind)


class SystemLine(Static):
    """Small one-line notices: compaction, interrupts, errors, server status."""

    def __init__(self, text: str, tone: str = "info"):
        super().__init__(text, markup=False)
        self.add_class(tone)


class ThinkingBlock(Collapsible):
    def __init__(self):
        self.body = Static("", classes="thinking-body", markup=False)
        super().__init__(self.body, title="Thinking…", collapsed=False, classes="thinking")
        self.text = ""
        self.started = time.monotonic()
        self.dirty = False

    def add(self, s: str) -> None:
        self.text += s
        self.dirty = True

    def flush(self) -> None:
        # While it streams, show the newest part: the body is capped at 24 rows, so the full text
        # would keep showing its first lines and hide everything the model writes after them.
        if self.dirty:
            text = self.text.strip()
            tail = "\n".join(text.splitlines()[-14:])[-1400:]
            self.body.update(("… " if len(tail) < len(text) else "") + tail)
            self.dirty = False
            secs = time.monotonic() - self.started
            self.title = f"Thinking… {secs:.0f}s · {len(self.text.split())} words"

    def finish(self) -> None:
        self.dirty = False
        self.body.update(self.text.strip())  # the whole text, scrollable, when you expand it
        secs = time.monotonic() - self.started
        words = len(self.text.split())
        self.title = f"Thought for {secs:.0f}s · {words} words"
        self.collapsed = True


class AssistantMessage(Markdown):
    def __init__(self):
        super().__init__("", classes="assistant")
        self.pending = ""
        self.full = ""

    def add(self, s: str) -> None:
        self.pending += s
        self.full += s

    async def flush(self) -> None:
        if self.pending:
            chunk, self.pending = self.pending, ""
            await self.append(chunk)


def _result_preview(name: str, result: str) -> str:
    try:
        d = json.loads(result)
    except ValueError:
        return result[-3000:]
    if not isinstance(d, dict):
        return result[-3000:]
    if d.get("error"):
        return "✗ " + str(d["error"])
    if d.get("image") and d.get("size"):
        return f"🖼 {d['image']}  ({d['size']}{', ' + d['shows'] if d.get('shows') else ''}) — attached for the model"
    if d.get("outline") and not d.get("content"):
        return d["outline"][:3000]
    if d.get("outline") and d.get("content"):
        return "Outline:\n" + d["outline"][:1500] + "\n\n" + "\n".join(d["content"].splitlines()[:15])
    for key in ("output", "diff", "matches", "content", "new_output", "log"):
        if d.get(key):
            text = str(d[key])
            lines = text.splitlines()
            return "\n".join(lines[-40:]) if len(lines) > 40 else text
    if name == "package_info":
        line = f"{d.get('name')} {d.get('latest')} ({d.get('released')}) · {d.get('ecosystem')}"
        if d.get("this_project_uses"):
            line += f"\nthis project: {d['this_project_uses']}"
        if d.get("deprecated"):
            line += f"\nDEPRECATED: {d['deprecated']}"
        return line
    if name == "github" and d.get("results") is not None:
        return "\n".join(f"• {r.get('repo')}  ★{r.get('stars', '')}  {r.get('path') or r.get('description') or ''}"
                         for r in d["results"])
    if name == "github" and d.get("releases") is not None:
        return "\n\n".join(f"{r['tag']} ({r['date']})\n{r['notes'][:600]}" for r in d["releases"])
    if d.get("readme"):
        return d["readme"][:3000]
    if name == "web_search":
        return "\n".join(f"• {r.get('title')}\n  {r.get('url')}" for r in d.get("results", []))
    if name == "web_extract":
        return "\n".join(f"• {r.get('title') or r.get('url')} ({len(r.get('content', ''))} chars)" for r in d.get("results", []))
    if name == "todo_list":
        return d.get("summary", "")
    return json.dumps(d, ensure_ascii=False, indent=1)[:3000]


_STR_FIELD = re.compile(r'"([A-Za-z_]\w*)"\s*:\s*"')
_BIG_FIELDS = ("content", "new_string", "command", "code", "old_string", "query", "text")


def _partial_json_string(raw: str) -> str:
    """Decode a JSON string body that may be cut off anywhere (mid-escape included)."""
    out, i = [], 0
    esc = {"n": "\n", "t": "\t", "r": "", '"': '"', "\\": "\\", "/": "/", "b": "", "f": ""}
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            break
        if ch == "\\":
            nxt = raw[i + 1:i + 2]
            if not nxt:
                break
            if nxt == "u":
                hexd = raw[i + 2:i + 6]
                if len(hexd) < 4:
                    break
                try:
                    out.append(chr(int(hexd, 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(esc.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def draft_view(name: str, args: str) -> tuple[str, str, str]:
    """(tool name, short label, text being written) from tool-call arguments still streaming in.
    Handles native JSON arguments and calls written as text (<function=…><parameter=…> or <tool_call>)."""
    label, body = "", ""
    if not name:
        last = max(args.rfind("<tool_call>"), args.rfind("<function="))
        if last > 0 and "<function=" in args[last:]:
            args = args[last:]
        m = re.search(r"<function=([\w.-]+)>", args) or re.search(r'"name"\s*:\s*"([\w.-]+)"', args)
        name = m.group(1) if m else "tool call"
        if "<parameter=" in args:
            params = re.findall(r"<parameter=([\w-]+)>\n?(.*?)(?=</parameter>|<parameter=|$)", args, re.S)
            vals = dict(params)
            label = (vals.get("path") or vals.get("file_path") or "").strip()
            if params:
                big = [v for k, v in params if k in _BIG_FIELDS]
                body = big[-1] if big else params[-1][1]
                if len(body) > 1 and body.endswith("\n") and len(params) and "</parameter>" in args:
                    body = body[:-1]  # the newline before </parameter> belongs to the markup
            return name, label, body
        m = re.search(r'"arguments"\s*:\s*(\{.*)', args, re.S)
        args = m.group(1) if m else args
    fields = [(m.group(1), m.end()) for m in _STR_FIELD.finditer(args)]
    vals = {k: _partial_json_string(args[pos:pos + 400]) for k, pos in fields if k in ("path", "file_path", "pattern")}
    label = vals.get("path") or vals.get("file_path") or vals.get("pattern") or ""
    big = [(k, pos) for k, pos in fields if k in _BIG_FIELDS]
    rest = [(k, pos) for k, pos in fields if k not in ("path", "file_path")]
    if big or rest:
        k, pos = (big or rest)[-1]
        body = _partial_json_string(args[pos:])
    return name, label, body


class DraftCard(Collapsible):
    """A tool call the model is still writing: shows the file content / command as it streams in."""

    def __init__(self):
        self.body = Static("", classes="tool-body", markup=False)
        self.name_, self.raw = "", ""
        self.started = time.monotonic()
        self.dirty = False
        self.lines = 0
        super().__init__(self.body, title="✍ drafting a tool call…", collapsed=False, classes="tool draft")

    def update_raw(self, name: str, args: str) -> None:
        self.name_, self.raw = name, args
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        name, label, body = draft_view(self.name_, self.raw)
        self.lines = len(body.splitlines())
        shown = body.splitlines()[-30:]
        self.body.update("\n".join(shown) if shown else "…")
        secs = time.monotonic() - self.started
        size = f"{self.lines} lines" if self.lines > 1 else f"{len(self.raw)} chars"
        self.title = f"✍ {name}  {short_label(label)}   writing… {size} · {secs:.0f}s"

    def finish(self) -> None:
        self.flush()
        name, label, _ = draft_view(self.name_, self.raw)
        self.title = f"✍ {name}  {short_label(label)}   drafted · {self.lines} lines"
        self.collapsed = True


def short_label(s: str, n: int = 70) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= n else "…" + s[-(n - 1):]


class ToolCard(Collapsible):
    def __init__(self, call_id: str, name: str, label: str, emoji: str):
        self.call_id, self.tool_name, self.label = call_id, name, label
        self.emoji = emoji
        self.body = Static("", classes="tool-body", markup=False)
        self.live = ""
        self.started = time.monotonic()
        self.dirty = False
        super().__init__(self.body, title=self._heading("…"), collapsed=True, classes="tool")

    def _heading(self, status: str) -> str:
        return f"{self.emoji} {self.tool_name}  {self.label}   {status}"

    def progress(self, text: str) -> None:
        self.live = (self.live + text)[-6000:]  # the panel shows the last 30 lines; keep only those
        self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            lines = self.live.splitlines()
            self.body.update("\n".join(lines[-30:]))
            self.dirty = False
            secs = time.monotonic() - self.started
            self.title = self._heading(f"running {secs:.0f}s")

    def finish(self, result: str, ok: bool, duration: float) -> None:
        self.body.update(_result_preview(self.tool_name, result) or "(no output)")
        self.title = self._heading(f"{'✓' if ok else '✗'} {duration:.1f}s")
        self.add_class("ok" if ok else "failed")
        self.dirty = False


class HeaderBar(Static):
    def render_state(self, model: str, status: str, used: int, total: int, tok_s: float, cwd: str, busy: bool,
                     approvals: str, mode: str = "normal") -> None:
        dot = {"ready": VERDIGRIS, "loading": BRONZE, "starting": BRONZE, "error": CINNABAR}.get(status, MUTED)
        t = Text()
        t.append(" ☤ ", style=f"bold {BRONZE}")
        t.append("Hermes Lite", style=f"bold {PARCHMENT}")
        t.append("   ")
        t.append(model, style=MUTED)
        t.append("   ● ", style=dot)
        t.append(status if not busy else "working", style=dot)
        frac = min(1.0, used / total) if total else 0.0
        cells = 16
        filled = round(frac * cells)
        t.append("    ")
        t.append("▰" * filled, style=BRONZE if frac < 0.75 else CINNABAR)
        t.append("▱" * (cells - filled), style="#3a4360")
        t.append(f" {used / 1000:.1f}k/{total / 1000:.0f}k", style=MUTED)
        if tok_s:
            t.append(f"    {tok_s:.1f} tok/s", style=PARCHMENT)
        if mode == "plan":
            t.append("    ")
            t.append(" PLAN MODE · /build to carry out · /plan off ", style=f"bold #161b29 on {BRONZE}")
        if approvals == "off":
            t.append("    yolo", style=f"bold {CINNABAR}")
        t.append(f"    {cwd}", style=MUTED)
        self.update(t)


class Sidebar(Vertical):
    def compose(self):
        yield Static("Plan", classes="side-title")
        yield Static("No plan yet. The agent writes one with todo_list on multi-step work.", id="plan", classes="side-body")
        yield Static("Background", classes="side-title")
        yield Static("Nothing running.", id="procs", classes="side-body")
        yield Static("Session", classes="side-title")
        yield Static("", id="session", classes="side-body")

    def show_todos(self, todos: list[dict]) -> None:
        if not todos:
            self.query_one("#plan", Static).update("No plan yet.")
            return
        marks = {"pending": ("○", MUTED), "in_progress": ("◉", BRONZE), "completed": ("✓", VERDIGRIS),
                 "cancelled": ("✗", CINNABAR)}
        ids = {t["id"] for t in todos}
        t = Text()
        for item in todos:
            mark, color = marks.get(item["status"], ("○", MUTED))
            indent = "   " if item.get("parent") in ids else ""
            style = f"strike {MUTED}" if item["status"] == "cancelled" else (
                MUTED if item["status"] == "completed" else PARCHMENT)
            t.append(f"{indent}{mark} ", style=color)
            t.append(item["content"] + "\n", style=style)
        self.query_one("#plan", Static).update(t)

    def show_procs(self, rows: list[tuple[str, str, str]]) -> None:
        if not rows:
            self.query_one("#procs", Static).update("Nothing running.")
            return
        t = Text()
        for pid, status, cmd in rows:
            t.append("● " if status == "running" else "○ ", style=VERDIGRIS if status == "running" else MUTED)
            t.append(f"{cmd}\n", style=PARCHMENT)
            t.append(f"  {pid} · {status}\n", style=MUTED)
        self.query_one("#procs", Static).update(t)

    def show_session(self, text: str) -> None:
        self.query_one("#session", Static).update(Text(text, style=MUTED))


class PromptInput(TextArea):
    """Enter sends. Shift+Enter, Alt+Enter or Ctrl+J insert a newline. Pasted text stays one message."""

    class Submitted(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    def __init__(self, **kw):
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="indent", **kw)
        self.sent: list[str] = []
        self.sent_pos = 0

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.app.action_interrupt()
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self.sent.append(text)
                self.sent_pos = len(self.sent)
                self.clear()
                self.post_message(self.Submitted(text))
            return
        if event.key == "ctrl+v":  # paste from the system clipboard (the terminal's own paste works too)
            event.prevent_default()
            event.stop()
            from .clipboard import paste
            text = paste()
            if text is None:
                text = getattr(self.app, "_clipboard", "") or ""
            if text:
                self.insert(text)
            return
        if event.key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "up" and self.cursor_location[0] == 0 and self.sent:
            event.prevent_default()
            self.sent_pos = max(0, self.sent_pos - 1)
            self.load_text(self.sent[self.sent_pos])
            return
        if event.key == "down" and self.cursor_location[0] == self.document.line_count - 1 and self.sent:
            event.prevent_default()
            self.sent_pos = min(len(self.sent), self.sent_pos + 1)
            self.load_text(self.sent[self.sent_pos] if self.sent_pos < len(self.sent) else "")
            return
        await super()._on_key(event)
