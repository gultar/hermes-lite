"""Hermes Lite terminal UI (Textual)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.theme import Theme
from textual.widgets import Static

from ..agent import Agent, list_sessions, load_session
from ..config import Config
from ..llm import ToolCall
from ..server import LlamaServer, build_command
from ..tools import REGISTRY
from ..tools.skills import catalog
from .screens import ApprovalScreen, ClarifyScreen, SecretScreen, SessionScreen, SkillBrowser
from .widgets import (AssistantMessage, DraftCard, HeaderBar, PromptInput, Sidebar, SystemLine, ThinkingBlock, ToolCard,
                      UserMessage)

HERMES_THEME = Theme(
    name="hermes", primary="#c8a04a", secondary="#6fb5a0", accent="#c8a04a", warning="#e0a84f",
    error="#d0705f", success="#6fb5a0", background="#161b29", surface="#1f2638", panel="#273047",
    foreground="#e7e1d1", dark=True,
    variables={"block-cursor-background": "#c8a04a", "block-cursor-foreground": "#161b29",
               "input-selection-background": "#c8a04a 35%", "footer-key-foreground": "#c8a04a"},
)

HELP = """Commands
  /new              start a fresh session              /resume [id]   pick or load a saved session
  /retry            re-run your last message           /undo          drop the last exchange
  /compress         compact the context now            /usage         context and speed
  /cwd <dir>        change the working directory       /think on|off  model thinking
  /yolo             toggle command approvals           /memory        show memory + user profile
  /skills           list skills  · /skills browse: pick from a catalogue   /tools  list tools
  /server [restart|reset] status / restart / retry your own settings      /quit          exit
  /vision [ram|vram|off]  vision encoder (screenshots): where it runs, or off; restarts llama-server
  /telegram [on|off|setup|forget]  use this session from Telegram too (status without an argument)
  /server handoff   give the GPU back to the server(s) it stopped (server.stop_others); your next message takes it back
  /forget-password  drop a sudo password you chose to remember
  /stream           what llama-server has sent for the current reply (raw; also tail -f ~/.hermes-lite/logs/stream.log)
  /plan [task]      plan mode: the model investigates and writes a plan, changes nothing   /plan off  leave it
  /build [fresh]    carry out .hermes/plan.md (fresh: in a new, clean session)
  /prompts          list the prompt files · /prompts edit|reset|undo <name> · /prompts reload
  /learn            review this session now and save what it learned as a skill
  /notes [update|rewrite]  the project notes (PROJECT_NOTES.md): show / add what changed now / rewrite them whole
  /copy [error]     copy the last answer (or error)    /export [file]  save the conversation as Markdown    select text with the mouse: it is copied when you let go
  !<command>        run a shell command yourself (not sent to the model)

Keys
  Enter send · Shift+Enter / Alt+Enter / Ctrl+J newline · Esc interrupt · Ctrl+B sidebar
  Ctrl+T show/hide thinking · Ctrl+N new session · Ctrl+R resume · Ctrl+Q quit
  Typing while Hermes works steers it: your message is delivered after the current tool results."""


class HermesApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "Hermes Lite"
    BINDINGS = [
        Binding("escape", "interrupt", "Interrupt", show=False),
        Binding("ctrl+c", "ctrl_c", "Interrupt / quit", show=False, priority=True),
        Binding("ctrl+n", "new_session", "New session", show=False),
        Binding("ctrl+r", "resume", "Resume", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=False),
        Binding("ctrl+t", "toggle_thinking", "Thinking", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, cfg: Config, cwd: str, start_server: bool = True, resume: Optional[str] = None,
                 telegram: bool = False, headless: bool = False):
        super().__init__()
        self.cfg = cfg
        self.server = LlamaServer(cfg)
        self.start_server = start_server
        self.telegram = None                 # the Telegram bridge while /telegram is on
        self.telegram_at_start = telegram
        self.headless = headless             # hermes-lite --telegram: no TUI, Telegram only
        self._remote_run = False             # the request running now came from Telegram
        self.agent = Agent(cfg, self, cwd=cwd)
        self.resume_id = resume
        self.server_ready = asyncio.Event()
        self.show_thinking = bool(cfg.get_path("ui.show_thinking", True))
        self.cur_thinking: Optional[ThinkingBlock] = None
        self.cur_answer: Optional[AssistantMessage] = None
        self.cards: dict[str, ToolCard] = {}
        self.status_text = ""
        self._ctrl_c_armed = False
        self.last_error = ""
        self._retry_after_restart = False
        self._last_auto_copy = ""
        self._progress_sample: tuple[float, float] | None = None

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Horizontal(id="main"):
            yield VerticalScroll(id="log")
            yield Sidebar(id="sidebar")
        with Horizontal(id="input-row"):
            yield Static("›", id="prompt-mark")
            yield PromptInput(id="prompt")
        yield Static("", id="hint")

    def on_mount(self) -> None:
        self.agent.loop = asyncio.get_running_loop()
        self.register_theme(HERMES_THEME)
        theme = self.cfg.get_path("ui.theme", "hermes")
        self.theme = theme if theme in self.available_themes else "hermes"
        self.query_one("#prompt", PromptInput).focus()
        self.set_interval(1 / 15, self._flush)
        self.set_interval(2.0, self._refresh_sidebar)
        self.set_interval(1.0, self._poll_progress)
        self._hint("Enter to send · Shift+Enter newline · select text to copy · Esc interrupts · /help")
        from .. import BUILD
        from .. import promptfiles
        self._line(f"Hermes Lite {BUILD}")
        self._announce_carryover()
        from .. import projectnotes
        notes_file = projectnotes.notes_path(self.cfg, self.agent.ctx.cwd)
        if notes_file is not None:
            if projectnotes.load(notes_file):
                self._line(f"📒 Project notes read: {notes_file} — the model starts from them (/notes shows them).", "ok")
            else:
                self._line(f"📒 No project notes yet: the model writes {notes_file.name} at the project root after its "
                           "first bit of work here (or now: /notes update).", "help")
        mine = [n for n, src, _ in promptfiles.listing() if src == "yours"]
        if mine:
            self._line(f"Your prompt versions in use: {', '.join(mine)} (/prompts)", "help")
        self._prompt_warnings_shown = set(promptfiles.WARNINGS)
        for w in promptfiles.WARNINGS:
            self._line("⚠ " + w, "error")
        self._refresh_header()
        self._refresh_sidebar()
        if self.telegram_at_start or self.cfg.get_path("telegram.auto_on", False):
            self.run_worker(self._telegram_on(quiet_if_unset=not self.telegram_at_start), group="telegram")
        if self.headless:
            import signal
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, lambda: self.run_worker(self.action_quit(), group="quit"))
                except (NotImplementedError, RuntimeError):
                    pass
        if self.resume_id:
            data = load_session(self.resume_id)
            if data:
                self._load(data)
            else:
                self._line(f"No saved session matches {self.resume_id!r}.", "error")
        self.run_worker(self._boot_server(), group="server")
        self._start_memory_guard()

    # ── helpers ─────────────────────────────────────────────────────────
    @property
    def log_view(self) -> VerticalScroll:
        return self.query_one("#log", VerticalScroll)

    def _at_bottom(self) -> bool:
        log = self.log_view
        return log.scroll_y >= log.max_scroll_y - 2

    def _mount(self, widget) -> None:
        follow = self._at_bottom()
        log = self.log_view
        if len(log.children) > 400:  # a long session: drop the oldest widgets (history stays in the session file)
            for old in list(log.children)[:100]:
                if old not in self.cards.values() and old is not self.cur_answer and old is not self.cur_thinking:
                    old.remove()
        log.mount(widget)
        if follow:
            self.call_after_refresh(self.log_view.scroll_end, animate=False)

    def _line(self, text: str, tone: str = "info") -> None:
        self._mount(SystemLine(text, tone))
        if self.headless:   # no screen: the terminal that started it shows these lines
            import sys
            print(("⚠ " if tone == "error" else "") + text, file=sys.__stdout__, flush=True)

    def _hint(self, text: str) -> None:
        self.status_text = text
        self.query_one("#hint", Static).update(Text(text, style="#5f6680"))

    def _refresh_header(self) -> None:
        used, total = self.agent.context_usage()
        cwd = self.agent.ctx.cwd.replace(str(Path.home()), "~")
        self.query_one(HeaderBar).render_state(
            getattr(self, "served_model", "") or self.cfg.get_path("model.name", "local"), self.server.status,
            used, total, self.agent.last_tok_s,
            cwd, self.agent.busy, self.cfg.get_path("agent.approvals", "smart"), self.agent.mode)

    def _refresh_sidebar(self) -> None:
        sb = self.query_one(Sidebar)
        procs = self.agent.ctx.state.get("procs", {})
        sb.show_procs([(p.id, p.status(), p.command[:28]) for p in list(procs.values())[-6:]])
        sb.show_session(f"{self.agent.title or 'new session'}\n{self.agent.session_id}\n"
                        f"{len(self.agent.messages)} messages")

    def _poll_progress(self) -> None:
        """While a request runs, read llama-server's own progress lines from its log."""
        import re
        import time as _t
        if not self.server.logfile.exists():
            return
        if not self.agent.busy:
            self._progress_sample = None
            self._check_orphan()
            return
        try:
            with open(self.server.logfile, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 6000))
                lines = fh.read().decode(errors="replace").splitlines()
        except OSError:
            return
        for line in reversed(lines):
            m = re.search(r"prompt processing, n_tokens = +(\d+), progress = ([\d.]+)(?:, t = +([\d.]+) s)?.*?([\d.]+) "
                          r"tokens per second", line)
            if m:
                # n_tokens = tokens actually read for THIS request (the cached part is not counted) and
                # the speed = n_tokens / the whole prompt time, fixed overheads included: with a cached
                # prompt it is a few tokens over a few seconds, i.e. a meaningless "0.3 tok/s".
                done, frac, speed = int(m.group(1)), float(m.group(2)), float(m.group(4))
                secs_in = float(m.group(3)) if m.group(3) else None
                if frac >= 0.995:  # finished reading: the model is writing (or about to)
                    self._progress_sample = None
                    took = f" in {secs_in:.0f} s" if secs_in else ""
                    note = (f"read {done:,} new tokens{took} ({speed:.0f} tok/s)" if done >= 512 else
                            f"read {done:,} new tokens{took}; the rest came from its cache")
                    self._hint(f"Server {note} · the model is writing · Esc to stop")
                    return
                now = _t.monotonic()
                eta = ""
                prev = self._progress_sample
                if prev and frac > prev[1] and now > prev[0]:
                    secs = (1 - frac) / ((frac - prev[1]) / (now - prev[0]))
                    eta = f" · about {secs / 60:.0f} min left" if secs >= 90 else f" · about {secs:.0f} s left"
                if not prev or frac != prev[1]:
                    self._progress_sample = (now, frac)
                rate = f"{speed:.0f} tok/s" if done >= 512 else f"{done:,} new tokens so far"
                self._hint(f"Server reading the prompt: {frac * 100:.0f}% · {rate}{eta} · Esc to stop")
                return
            m = re.search(r"n_gen = +(\d+), tg = +([\d.]+) t/s", line)
            if m:
                self._progress_sample = None
                quiet = _t.monotonic() - (self.agent.llm.last_data or _t.monotonic())
                extra = ""
                if quiet > 20:
                    extra = (f" · nothing received for {quiet / 60:.0f} min" if quiet >= 90 else
                             f" · nothing received for {quiet:.0f} s")
                    if not self.agent.llm.client_tools:
                        extra += " (llama-server holds tool calls back; model.tool_parsing: client streams them)"
                rx = self.agent.llm.rx
                got = sum(rx.values())
                self._hint(f"Server writing: {m.group(1)} tokens · {float(m.group(2)):.1f} tok/s · received {got // 4} "
                           f"tokens' worth{extra} · /stream shows it · Esc to stop")
                return
            if "release:" in line or "stop processing" in line:
                return

    def _check_orphan(self) -> None:
        """llama-server still generating although nothing is waiting for it (an abandoned request
        keeps the GPU/CPU busy and slows the next one): say so once."""
        import os
        import re
        import time as _t
        try:
            if _t.time() - os.path.getmtime(self.server.logfile) > 5:
                return
            with open(self.server.logfile, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 3000))
                tail = fh.read().decode(errors="replace").splitlines()
        except OSError:
            return
        for line in reversed(tail):
            if "release:" in line or "stop processing" in line or "prompt processing" in line:
                return
            if m := re.search(r"n_gen = +(\d+)", line):
                self._orphan_polls = getattr(self, "_orphan_polls", 0) + 1
                if self._orphan_polls >= 4 and not getattr(self, "_orphan_warned", False):
                    self._orphan_warned = True
                    self._line(f"llama-server is still writing a reply nobody is waiting for ({m.group(1)} tokens so far). "
                               "It slows everything down; /server restart stops it.", "error")
                return

    async def _flush(self) -> None:
        follow = self._at_bottom()
        changed = False
        if self.cur_thinking is not None and self.cur_thinking.dirty and self.cur_thinking.is_mounted:
            self.cur_thinking.flush()
            changed = True
        if self.cur_answer is not None and self.cur_answer.pending and self.cur_answer.is_mounted:
            await self.cur_answer.flush()
            changed = True
        for card in list(self.cards.values()) + list(getattr(self, "drafts", {}).values()):
            if card.dirty and card.is_mounted:
                card.flush()
                changed = True
        if changed and follow:
            self.log_view.scroll_end(animate=False)

    def _end_thinking(self) -> None:
        if self.cur_thinking is not None and not self.cur_thinking.collapsed:
            self.cur_thinking.finish()

    # ── memory guard ────────────────────────────────────────────────────
    def _start_memory_guard(self) -> None:
        from ..config import home as _home
        from ..memwatch import MemoryWatchdog
        self.memguard = None
        from .. import oscompat
        if not self.cfg.get_path("agent.memory_guard", True) or not oscompat.memory_supported():
            return

        def label(pid: int) -> str:
            st = self.agent.ctx.state
            for p in st.get("procs", {}).values():
                if p.popen.pid == pid:
                    return f"background: {p.command}"
            if pid in st.get("tool_pids", set()):
                return st.get("current_command", f"process {pid}")
            return oscompat.process_cmdline(pid)[:200] or f"process {pid}"

        def on_kill(pid: int, what: str, rss: int, avail: int) -> None:
            from ..memwatch import gib
            msg = (f"Stopped because the computer was running out of memory: it was using {gib(rss)} with only "
                   f"{gib(avail)} left. Do not run it the same way again: limit its output or memory, run it "
                   "in the background, or ask the user.")
            self.agent.ctx.state.setdefault("killed_for_memory", {})[pid] = msg
            self.call_from_thread(self._on_memory_kill, what, rss, avail)

        def on_warn(text: str) -> None:
            self.call_from_thread(self._line, text, "error")

        self.memguard = MemoryWatchdog(
            _home() / "logs" / "memory.log",
            min_free_gib=float(self.cfg.get_path("agent.memory_guard_min_free_gib", 1.5)),
            server_pid=self.server.pid, label=label, on_kill=on_kill, on_warn=on_warn).start()

    def _on_memory_kill(self, what: str, rss: int, avail: int) -> None:
        from ..memwatch import gib
        self._line(f"Memory guard: the computer was nearly out of RAM ({gib(avail)} left), so Hermes Lite "
                   f"stopped “{what[:120]}”, which was using {gib(rss)}.", "error")
        self.agent._notify(f"[Memory guard stopped a process to save the system: “{what[:200]}” was using "
                           f"{gib(rss)} with only {gib(avail)} of RAM left. Do not start it the same way again.]")

    # ── server ──────────────────────────────────────────────────────────
    async def _boot_server(self) -> None:
        self.agent.llm.client_tools = None  # re-check tool parsing, vision and thinking control
        self.agent.llm.vision = None
        self.agent.llm.control_ok = None
        if not self.start_server:
            state = await self.server.health()
            self.server.status = state
            if state == "ready":
                self.server_ready.set()
            else:
                self._line(f"No llama-server answering at {self.server.base} (started with --no-server).", "error")
            self._refresh_header()
            return

        def on_status(state: str, detail: str) -> None:
            self._refresh_header()
            if state in ("starting", "loading"):
                self._hint("Loading the model… you can type already; your message waits for it.")
            elif state == "tuning":
                if detail:
                    self._hint(detail)
                    if "trying" in detail and "less context" in detail:
                        self._hint(f"llama-server: {detail}…")  # one hint, not a line per 1k step
            elif state == "ready":
                self._hint("Model ready · Enter to send · select text to copy · Esc interrupts · /help")
                if not detail.startswith(("already running", "restarted")):
                    found = f", found {self.server.saved_on}" if self.server.saved_on else ""
                    self._line(f"llama-server: {self.server.rung_label()} (saved setting{found}; reused at every "
                               "launch without retesting · /server reset searches again).", "ok")
            elif state == "warning":
                self._line(f"⚠ {detail}", "error")
            elif state == "error":
                self._line(f"llama-server: {detail}\nConfig: {self.cfg['_path']}", "error")

        from ..server import RAM_WARNINGS

        def flush_warnings() -> None:
            for w in RAM_WARNINGS:
                self._line(w, "error")
            RAM_WARNINGS.clear()
        flush_warnings()
        ok = await self.server.ensure(on_status, warmup_body=self._warmup_body())
        flush_warnings()   # the ones found while building the command
        if ok:
            self.server_ready.set()
            running = await self.server.served_model()
            self.served_model = Path(running).stem if running else ""
            if running and Path(running).name != Path(self.cfg["server"]["model_path"]).expanduser().name:
                self._line(f"The server runs {Path(running).name}, not the model in your config "
                           f"({Path(self.cfg['server']['model_path']).name}). /server restart replaces it.", "error")
            if self._retry_after_restart:
                self._retry_after_restart = False
                self._retry()
        self._refresh_header()

    def _warmup_body(self) -> dict:
        """A request the size of a real first turn: Hermes' system prompt and every tool schema."""
        body = self.agent.llm._body([self.agent.messages[0], {"role": "user", "content": "Reply with OK."}],
                                    self.agent.tools_schema(), 1)
        body["stream"] = False
        body.pop("stream_options", None)
        return body

    # ── agent UI protocol ───────────────────────────────────────────────
    def event(self, kind: str, **data: Any) -> None:
        handler = getattr(self, f"_ev_{kind}", None)
        if handler:
            handler(**data)
        if self.telegram and self.telegram.active:
            self._telegram_event(kind, data)

    def _ev_assistant_start(self) -> None:
        self._orphan_warned, self._orphan_polls = False, 0
        self.cur_thinking, self.cur_answer = None, None
        self.drafts = {}
        self._refresh_header()
        self._hint("Thinking…  Esc to interrupt · type to steer")

    def _ev_reasoning(self, text: str) -> None:
        if not self.show_thinking:
            self._hidden_thought = getattr(self, "_hidden_thought", 0) + len(text)
            if self._hidden_thought // 2000 != (self._hidden_thought - len(text)) // 2000:
                self._hint(f"Thinking (hidden — Ctrl+T shows it)… {self._hidden_thought // 4} tokens so far")
            return
        if self.cur_thinking is None:
            self.cur_thinking = ThinkingBlock()
            self._mount(self.cur_thinking)
        self.cur_thinking.add(text)

    def _ev_content(self, text: str) -> None:
        self._end_thinking()
        if self.cur_answer is None:
            if not text.strip():
                return
            text = text.lstrip()
            self.cur_answer = AssistantMessage()
            self._mount(self.cur_answer)
            self._hint("Writing…  Esc to interrupt")
        self.cur_answer.add(text)

    def _ev_loop_stopped(self, excerpt: str, how: str) -> None:
        what = {"ended": "ended its thinking; it goes on with its answer",
                "retry": "stopped the reply; resending it with a note to act, and a smaller thinking budget",
                "gave_up": "stopped it again and gave up on this message: tell it which way to go"}[how]
        self._line(f"🔁 Thinking loop (repeating: “{excerpt[:90]}…”): {what}. "
                   "loop_guard.enabled: false in config.yaml turns this off.", "error" if how == "gave_up" else "info")

    def _ev_plan_saved(self, path: str, steps: int) -> None:
        self._line(f"📝 Plan saved to {path} ({steps} steps). Ask for changes, edit the file, or type /build "
                   "(/build fresh for a clean session).", "ok")

    def _ev_images_attached(self, labels: str, count: int) -> None:
        self._line(f"🖼 Shown to the model once: {labels}. After its reply the image is replaced by its notes "
                   "(keeps the context small).")

    def _ev_tool_draft(self, index: int, name: str, args: str) -> None:
        """The model is writing a tool call (file content, a patch, a command): show it as it streams."""
        drafts = getattr(self, "drafts", None)
        if drafts is None:
            drafts = self.drafts = {}
        card = drafts.get(index)
        if card is None:
            self._end_thinking()
            self.cur_answer = None
            card = drafts[index] = DraftCard()
            self._mount(card)
            self._hint(f"Writing a {name or 'tool'} call…  Esc to interrupt")
        card.update_raw(name, args)

    def _ev_assistant_end(self, tokens: int, tok_s: float, context: int, ctx_len: int, seconds: float,
                          finish: str) -> None:
        for card in getattr(self, "drafts", {}).values():
            if card.is_mounted:
                if len(card.raw) < 400:  # a short call: its tool card says it all
                    card.remove()
                else:
                    card.finish()
        self.drafts = {}
        self._end_thinking()
        self._refresh_header()
        if finish == "length":
            self._line("The reply hit max_tokens and was cut off (raise model.max_tokens in config.yaml).", "error")

    def _ev_tool_start(self, id: str, name: str, label: str, emoji: str, args: dict) -> None:
        self._end_thinking()
        self.cur_answer = None
        card = ToolCard(id, name, label, emoji)
        self.cards[id] = card
        self._mount(card)
        self._hint(f"Running {name}…  Esc to interrupt")

    def _ev_tool_progress(self, id: str, text: str) -> None:
        if card := self.cards.get(id):
            card.progress(text)

    def _ev_tool_end(self, id: str, name: str, result: str, ok: bool, duration: float) -> None:
        if card := self.cards.pop(id, None):
            card.finish(result, ok, duration)
        if name in ("terminal", "process_manage"):
            self._refresh_sidebar()

    def _ev_todos(self, todos: list) -> None:
        self.query_one(Sidebar).show_todos(todos)

    def _ev_final(self, text: str) -> None:
        pass

    def _ev_status(self, text: str) -> None:
        self._hint(text)
        if text.startswith("Compaction cancelled"):
            self._compaction_cancelled = True
            self._line(text)

    def last_error_recent(self) -> bool:
        return bool(self.last_error) and self.last_error.startswith("Compaction failed")

    def _ev_error(self, text: str) -> None:
        self.last_error = text
        if "llama-server" in text:
            self.run_worker(self._diagnose_server(text), group="server")
        else:
            self._line(text, "error")

    async def _diagnose_server(self, text: str) -> None:
        """A failed request: say whether the server is still alive and show its last log lines."""
        state = await self.server.health()
        self.server.status = state if state != "down" else "error"
        if state == "ready":
            self.server_ready.set()
        else:
            self.server_ready.clear()
        if state != "ready" and self.start_server and self.server._memory_crash() and self.server.step_down():
            # Out of GPU memory mid-session: restart on the next safer setting and resend the message.
            self._line(f"llama-server ran out of GPU memory. Restarting it with: {self.server.rung_label()} "
                       "— your message will be sent again automatically.")
            self._retry_after_restart = True
            await self._boot_server()
            return
        tail = self.server.log_tail(15)
        msg = text + (f"\n\nServer is {state}." if state != "ready" else "")
        if state != "ready" and (busy := self.server.gpu_competitors()):
            msg += "\n" + busy
        try:
            code = int(self.server.exitfile.read_text().strip())
        except (OSError, ValueError):
            code = None
        if state != "ready" and code in (137, -9):
            msg += ("\nllama-server was killed by the system (exit 137): almost always because RAM ran out "
                    "(the kernel's out-of-memory killer). Close big apps or another model server, then /server restart.")
        if tail:
            msg += f"\nLast lines of {self.server.logfile}:\n{tail}"
        if state != "ready":
            msg += "\n\n/server restart starts it again. Ctrl+C copies selected text; /copy copies this error."
        self.last_error = msg
        self._line(msg, "error")
        self._refresh_header()

    def _ev_interrupted(self) -> None:
        self._end_thinking()
        self._line("Interrupted.")

    def _ev_compacted(self, reason: str, summary: str, removed: int) -> None:
        self._line(f"Context compacted ({reason}): {removed} earlier messages folded into a checkpoint summary, "
                   "with your requests, files touched and last commands kept word for word.", "ok")
        self._refresh_header()

    def _ev_pruned(self, items: int, before: int, after: int) -> None:
        self._line(f"✂ Context: trimmed {items} old tool outputs ({before // 1000}k → {after // 1000}k tokens). "
                   "Every message and tool call is still there word for word; no summary needed.", "ok")
        self._refresh_header()

    def _announce_carryover(self) -> None:
        """What the model brings back from earlier sessions here: its notes and open todo items."""
        from ..tools import notes as nt
        from ..tools.base import disabled_tools
        state = self.agent.ctx.state
        left = [t for t in state.get("todos") or [] if t.get("status") in ("pending", "in_progress")]
        if state.get("todos_restored") and left:
            self.query_one(Sidebar).show_todos(state["todos"])
            self._line(f"📋 {len(left)} unfinished todo item{'s are' if len(left) != 1 else ' is'} back from your "
                       "last session here.", "ok")
        if "notes" not in disabled_tools(self.cfg):
            n = len(nt.read(nt.notes_file(self.agent.ctx)))
            if n:
                self._line(f"🗒 {n} working note{'s' if n != 1 else ''} loaded ({nt.notes_file(self.agent.ctx)}).", "ok")

    def _ev_notes_start(self, path: str) -> None:
        self._hint("Updating the project notes… · Esc skips")

    def _ev_notes_end(self, outcome: str, path: str, detail: str = "") -> None:
        if outcome == "saved":
            self._line(f"📒 Project notes updated ({detail}): {path} · /notes shows them", "ok")
        elif outcome == "unchanged":
            self._hint("Project notes: nothing new to add.")
        elif outcome == "skipped":
            self._line("Project notes not updated (skipped).")
        elif outcome == "failed":
            self._line(f"Project notes not updated: {detail}", "error")

    def _ev_review_start(self) -> None:
        self._line("Reviewing this session for a skill worth saving… (Esc to skip, or just type)")

    def _ev_review_end(self, outcome: str, saved: list) -> None:
        self._end_thinking()
        if outcome == "saved":
            self._line("Skill saved: " + "; ".join(saved) + ". It is listed from the next session on (/new).", "ok")
        elif outcome == "skipped":
            self._line("Skill review skipped.")
        else:
            self._line("Skill review: nothing worth saving this time.")

    def _ev_steered(self, text: str) -> None:
        pass

    def _ev_notification(self, text: str, idle: bool) -> None:
        self._mount(UserMessage(text.splitlines()[0], "notification"))
        if idle:  # wake the agent so it can react to the finished process
            self.agent.notifications.remove(text)
            self._start_run(text, synthetic=True)

    def _ev_idle(self) -> None:
        self._refresh_header()
        self._refresh_sidebar()
        self._hint("Enter to send · Shift+Enter newline · select text to copy · Esc interrupts · /help")
        if self.agent.steer_queue:  # steers that arrived during the final answer become the next message
            text = "\n\n".join(self.agent.steer_queue)
            self.agent.steer_queue.clear()
            self._start_run(text)

    def _close_screen(self, screen, value) -> None:
        """Answered on the phone: close the same question at the computer."""
        try:
            if screen.is_attached and self.screen is screen:
                screen.dismiss(value)
        except Exception:
            pass

    async def approve(self, command: str, reason: str) -> str:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        screen = ApprovalScreen(command, reason)
        self.push_screen(screen, lambda v: fut.done() or fut.set_result(v or "deny"))
        msg = None
        if self.telegram and self.telegram.active:
            try:
                msg = await self.telegram.ask_approval(command, reason, fut)
            except Exception as exc:
                self._line(f"Telegram: could not send the approval ({exc}).", "error")
        verdict = await fut
        self._close_screen(screen, verdict)
        if msg is not None and self.telegram:
            await self.telegram.close_approval(msg, verdict)
        return verdict

    async def ask_secret(self, prompt: str, command: str) -> tuple:
        if self.telegram and self.telegram.active and (self._remote_run or self.headless):
            # a sudo password is never asked for through the chat
            await self.telegram.send("🔒 This command needs your sudo password, which is only ever typed at the "
                                     f"computer, so it was refused:\n{command[:500]}")
            return None, False
        if self.telegram and self.telegram.active:
            await self.telegram.send("🔒 Waiting for your sudo password at the computer (it is never asked for here).")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(SecretScreen(prompt, command),
                         lambda v: fut.done() or fut.set_result(v if v else (None, False)))
        return await fut

    async def clarify(self, questions: list) -> list:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        screen = ClarifyScreen(questions)
        self.push_screen(screen, lambda v: fut.done() or fut.set_result(v or []))
        answer = None
        if self.telegram and self.telegram.active:
            from ..telegram import format_questions, parse_answers
            await self.telegram.send(format_questions(questions))
            answer = self.telegram.expect_answer()
            answer.add_done_callback(lambda f: None if f.cancelled() or fut.done()
                                     else fut.set_result(parse_answers(f.result(), questions)))
        result = await fut
        self._close_screen(screen, result)
        if answer is not None and not answer.done():
            answer.cancel()
        return result

    # ── running ─────────────────────────────────────────────────────────
    def _start_run(self, text: str, synthetic: bool = False) -> None:
        if self.server.status == "handed over":  # /server handoff gave the GPU away: take it back
            self.server.status = "starting"
            self._line("Taking the GPU back: starting Hermes Lite's llama-server again…")
            self.run_worker(self._boot_server(), group="server")

        async def go():
            if not self.server_ready.is_set():
                self._hint("Waiting for the model to finish loading…")
                await self.server_ready.wait()
            await self.agent.run(text, synthetic=synthetic)
        self.run_worker(go(), group="agent")

    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        text = message.text
        if text.startswith("/"):
            await self._command(text)
            return
        if text.startswith("!"):
            self._shell(text[1:].strip())
            return
        if self.agent.busy and self.agent.reviewing:
            # Typing during a skill review: skip the review and send the message right after.
            self.agent.steer(text)
            self._mount(UserMessage(text))
            self.agent.interrupt()
            return
        if self.telegram and self.telegram.active:
            asyncio.create_task(self.telegram.send("🖥 " + text[:1500]))
        if self.agent.busy:
            self.agent.steer(text)
            self._mount(UserMessage(text, "steer"))
            return
        self._remote_run = False
        self._mount(UserMessage(text))
        self._start_run(text)

    def _shell(self, command: str) -> None:
        if not command:
            return
        self._mount(UserMessage("! " + command, "steer"))
        call = ToolCall(id="bang_" + uuid.uuid4().hex[:8], name="terminal",
                        arguments=json.dumps({"command": command}), parsed={"command": command})

        async def go():
            await self.agent._run_tools([call])
            for card in self.query(ToolCard):
                if card.call_id == call.id:
                    card.collapsed = False
            self._hint("Enter to send · Shift+Enter newline · select text to copy · Esc interrupts · /help")
        self.run_worker(go(), group="shell")

    def _browse_skills(self) -> None:
        from .. import skillcatalog as sc
        home = self.agent.home
        items = sc.entries(self.cfg, home)
        if not items:
            self._line("No catalogue found. It reads " + ", ".join(sc.DEFAULT_SOURCES) + " — or set skills.catalog "
                       "to folders of skills, e.g. a clone of Hermes Agent: skills.catalog: [~/src/hermes-agent/"
                       "optional-skills]", "help")
            return

        def toggle(e: dict) -> str:
            try:
                if e["removable"]:
                    sc.remove(e["name"], home)
                    e["added"] = e["removable"] = False
                    return f"Removed {e['name']}."
                if e["added"]:
                    return f"{e['name']} is already one of your skills (not added from here, so not removed here)."
                dest = sc.add(e, home)
                e["added"] = e["removable"] = True
                return f"Added {e['name']} → {dest}"
            except OSError as exc:
                return f"{e['name']}: {exc}"

        def done(changes: list | None) -> None:
            changes = [c for c in (changes or []) if c.startswith(("Added", "Removed"))]
            if changes:
                self._reload_prompts()   # the skill list is part of the system prompt
                self._line("\n".join(changes) + "\nThe model sees the new skill list from your next message.", "ok")
        self.push_screen(SkillBrowser(items, toggle), done)

    # ── Telegram ────────────────────────────────────────────────────────
    async def _telegram_command(self, arg: str) -> None:
        from .. import telegram as tg
        sub = (arg.split() or ["status"])[0].lower()
        home = self.agent.home
        if sub == "setup":
            await self._telegram_setup()
        elif sub in ("on", "start"):
            await self._telegram_on()
        elif sub in ("off", "stop"):
            await self._telegram_off()
        elif sub == "forget":
            await self._telegram_off(quiet=True)
            state = tg.load_state(home)
            for k in ("owner_id", "chat_id"):
                state.pop(k, None)
            tg.save_state(home, state)
            self._line("Telegram: unpaired. /telegram on shows a new pairing code.", "ok")
        else:
            st = tg.load_state(home)
            on = bool(self.telegram and self.telegram.active)
            self._line(f"Telegram: {'on' if on else 'off'}"
                       + (f" (@{self.telegram.username})" if on else "")
                       + f" · token {'set' if tg.token(home) else 'not set'}"
                       + f" · {'paired' if st.get('owner_id') else 'not paired'}\n"
                       "/telegram setup (bot token from @BotFather) · /telegram on · /telegram off · "
                       "/telegram forget (pair another account)", "help")

    async def _telegram_setup(self) -> bool:
        from .. import telegram as tg
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(SecretScreen("Telegram bot token", "Create a bot with @BotFather in Telegram (/newbot); "
                                      "it gives you a token like 123456789:AA…", placeholder="Bot token (hidden)",
                                      remember="", send="Save"),
                         lambda v: fut.done() or fut.set_result(v if v else (None, False)))
        value, _ = await fut
        value = (value or "").strip()
        if not value:
            return False
        if not tg.TOKEN_RE.match(value):
            self._line("That does not look like a bot token (digits, a colon, then ~35 characters).", "error")
            return False
        tg.save_state(self.agent.home, {**tg.load_state(self.agent.home), "token": value})
        self._line(f"Telegram bot token saved in {tg.state_path(self.agent.home)} (readable only by you).", "ok")
        return True

    async def _telegram_on(self, quiet_if_unset: bool = False) -> None:
        from .. import telegram as tg
        if self.telegram and self.telegram.active:
            self._line(f"Telegram is already on (@{self.telegram.username}).", "help")
            return
        home = self.agent.home
        if not tg.token(home):
            if quiet_if_unset or self.headless or not await self._telegram_setup():
                if self.headless:
                    self._line("No Telegram bot token: run hermes-lite --telegram in a terminal to set one.", "error")
                    self.exit()
                return
        bridge = tg.Bridge(home, self._remote_message, self._remote_command, self._line)
        try:
            name = await bridge.start()
        except Exception as exc:
            await bridge.client.aclose()
            self._line(f"Telegram did not start: {exc}", "error")
            if self.headless:
                self.exit()
            return
        self.telegram = bridge
        if bridge.owner:
            self._line(f"📱 Telegram on: @{name} talks to this session (/telegram off stops it).", "ok")
        else:
            self._line(f"📱 Telegram on. To pair your account, send this code to @{name}: {bridge.pair_code}\n"
                       "Only that account will be answered.", "ok")

    async def _telegram_off(self, quiet: bool = False) -> None:
        if self.telegram:
            await self.telegram.stop()
            self.telegram = None
            if not quiet:
                self._line("📱 Telegram off.", "ok")
        elif not quiet:
            self._line("Telegram is not on.", "help")

    async def _remote_message(self, text: str) -> None:
        self._mount(UserMessage("📱 " + text))
        if self.agent.busy:
            self.agent.steer(text)
            await self.telegram.send("↪ Got it: the agent reads it after its current step.")
            return
        self._remote_run = True
        self._start_run(text)

    async def _remote_command(self, cmd: str) -> str:
        if cmd in ("help", "start"):
            return ("Write to me as you would in Hermes Lite: your message goes to this session.\n"
                    "/stop  interrupt what it is doing\n/status  what it is doing, context used, folder\n"
                    "/new  start a new session\nCommand approvals come as buttons. A sudo password is never "
                    "asked for here.")
        if cmd in ("stop", "esc", "cancel"):
            if self.agent.busy:
                self.agent.interrupt()
                return "⏹ Interrupting."
            return "Nothing is running."
        if cmd == "status":
            used, total = self.agent.context_usage()
            return (f"{'⏳ working' if self.agent.busy else '💤 idle'} · context {used // 1000}k/{total // 1000}k · "
                    f"{self.agent.ctx.cwd} · llama-server {self.server.status}")
        if cmd == "new":
            await self.action_new_session()
            return "🆕 New session."
        return f"Unknown command /{cmd}. /help"

    def _telegram_event(self, kind: str, data: dict) -> None:
        tg = self.telegram

        def spawn(coro) -> None:
            async def safe():
                try:
                    await coro
                except Exception as exc:
                    self._line(f"Telegram: {exc}", "error")
            asyncio.create_task(safe())
        if kind == "assistant_start":
            tg.typing(True)
        elif kind == "tool_start":
            spawn(tg.progress(f"{data.get('emoji', '🔧')} {data.get('name')}: {str(data.get('label') or '')[:120]}"))
        elif kind == "final" and str(data.get("text") or "").strip():
            spawn(tg.send(data["text"], markdown=True))
        elif kind == "error":
            spawn(tg.send("⚠ " + str(data.get("text") or "")[:1500]))
        elif kind == "interrupted":
            spawn(tg.send("Interrupted."))
        elif kind == "images_attached":
            last = self.agent.messages[-1] if self.agent.messages else {}
            for path in last.get("_images") or []:
                spawn(tg.send_photo(path, data.get("labels", "")))
        elif kind == "idle":
            tg.typing(False)
            spawn(tg.progress_done("✓ done"))

    async def _vision(self, arg: str) -> None:
        """/vision: show where the vision encoder runs; /vision ram|vram|off: change it and restart."""
        from ..config import set_value
        from ..server import find_mmproj, ram_budget, ram_plan, vision_device
        s = self.cfg["server"]
        if arg not in ("ram", "vram", "gpu", "cpu", "off", "none"):
            mm = find_mmproj(self.cfg)
            where = ("off (server.mmproj: none)" if str(s.get("mmproj", "none")).lower() in ("none", "off", "")
                     else f"set, but no file found (server.mmproj: {s.get('mmproj')})" if not mm
                     else f"{mm.name} in {'VRAM (GPU)' if ram_plan(self.cfg)[2] else 'RAM (CPU)'}")
            b = ram_budget(self.cfg)
            ram = (f"RAM: {b['total'] / 2**30:.1f} GB = your apps {b['apps'] / 2**30:.1f} + model "
                   f"{b['model'] / 2**30:.1f} + {max(b['spare'], 0) / 2**30:.1f} left for the encoder and prompt "
                   "cache (the model counts as \"cache\" in system monitors, not \"used\")\n") if b else ""
            self._line(f"Vision encoder: {where}\nServer sees images: "
                       f"{ {True: 'yes', False: 'no', None: 'not checked yet'}[self.agent.llm.vision] }\n{ram}"
                       "/vision ram   on the CPU: no VRAM used, context untouched, slower image reading\n"
                       "/vision vram  on the GPU: fast, but ~0.9 GB of VRAM, so the context may be lowered\n"
                       "/vision off   no encoder (screenshot and view_image are left out)", "help")
            return
        device = {"gpu": "vram", "cpu": "ram", "none": "off"}.get(arg, arg)
        path = Path(self.cfg.get("_path") or "")
        changes: dict[str, str] = {}
        if device == "off":
            changes["mmproj"] = "none"
        else:
            changes["mmproj_device"] = device
            if str(s.get("mmproj", "none")).lower() in ("none", "off", ""):
                changes["mmproj"] = "auto"
                if not find_mmproj(Config({**self.cfg, "server": {**s, "mmproj": "auto"}})):
                    self._line("No vision encoder (mmproj*.gguf) next to the model. Download the one made for "
                               "your model, then: hermes-lite --set server.mmproj=/path/to/mmproj-F16.gguf", "error")
                    return
        try:
            from ..rollback import snapshot_config
            snapshot_config(path)  # so `hermes-lite --rollback config` undoes it
            for k, v in changes.items():
                set_value(path, f"server.{k}", v)
                s[k] = v
        except (OSError, ValueError) as e:
            self._line(f"Could not change {path}: {e}", "error")
            return
        self._line(f"Vision {'off' if device == 'off' else 'encoder in ' + device.upper()} "
                   f"({', '.join(f'server.{k}: {v}' for k, v in changes.items())}). Undo: hermes-lite --rollback config",
                   "ok")
        if not self.start_server:
            self._line("Started with --no-server: restart your llama-server yourself to apply it.", "help")
            return
        if self.agent.busy:
            self.agent.interrupt()

        async def again():
            self.server_ready.clear()
            self._hint("Stopping llama-server…")
            await self.server.stop_any()
            self._line("Restarting llama-server with the new vision setting…")
            await self._boot_server()
        self.run_worker(again(), group="server")

    # ── slash commands ──────────────────────────────────────────────────
    async def _command(self, line: str) -> None:
        cmd, _, arg = line[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("help", "h", "?"):
            self._line(HELP, "help")
        elif cmd in ("quit", "exit", "q"):
            await self.action_quit()
        elif cmd in ("new", "clear", "reset"):
            await self.action_new_session()
        elif cmd in ("resume", "sessions"):
            if arg:
                data = load_session(arg)
                self._load(data) if data else self._line(f"No session matches {arg!r}.", "error")
            else:
                await self.action_resume()
        elif cmd == "plan":
            if arg.lower() in ("off", "stop", "cancel"):
                self.agent.stop_plan()
                self._line("Plan mode off.", "ok")
            elif self.agent.busy:
                self._line("Wait for the current turn to finish, or press Esc first.", "error")
            else:
                self.agent.start_plan()
                self._line("📝 Plan mode: the model can read, search and run read-only commands, but changes nothing. "
                           "It ends with a plan saved to .hermes/plan.md. Discuss it, edit the file if you like, then "
                           "/build (or /build fresh for a clean session). /plan off leaves plan mode.", "ok")
                if arg:
                    self._mount(UserMessage(arg))
                    self._start_run(arg)
            self._refresh_header()
        elif cmd == "build":
            if self.agent.busy:
                self._line("Wait for the current turn to finish, or press Esc first.", "error")
            else:
                from ..planmode import load_plan, plan_path
                if not load_plan(self.agent.ctx.cwd) and self.agent.mode == "plan" and len(self.agent.messages) > 2:
                    # Planning happened but no plan was written: ask for it (tools off), then build.
                    self._line("No plan written yet: asking the model to write it now from what it found, then "
                               "building it.", "ok")
                    fresh = arg.lower() == "fresh"

                    async def plan_then_build():
                        from .. import promptfiles as pf
                        if not self.server_ready.is_set():
                            await self.server_ready.wait()
                        self.agent.force_plan = True
                        await self.agent.run(pf.get("plan_write_now"), synthetic=True)
                        if load_plan(self.agent.ctx.cwd):
                            await self._command("/build" + (" fresh" if fresh else ""))
                        else:
                            self._line("The model still didn't write a plan in the expected sections. Ask it to, "
                                       "or /plan off to work without one.", "error")
                    self.run_worker(plan_then_build(), group="agent")
                elif not load_plan(self.agent.ctx.cwd):
                    self._line(f"No plan at {plan_path(self.agent.ctx.cwd)} yet. Start with /plan <task>.", "error")
                else:
                    if arg.lower() == "fresh":
                        written = self.agent.plan_written
                        await self.action_new_session()
                        self.agent.plan_written = written
                    message = self.agent.build_message()
                    if not self.agent.title:
                        import re as _re
                        goal = _re.search(r"#+\s*goal\s*\n+(.+)", load_plan(self.agent.ctx.cwd) or "", _re.I)
                        self.agent.title = "build: " + (goal.group(1).strip()[:50] if goal else "plan")
                    self._refresh_header()
                    self._mount(UserMessage("/build — carrying out .hermes/plan.md" + (" in a fresh session" if arg.lower() == "fresh" else "")))
                    self._start_run(message)
        elif cmd == "prompts":
            from .. import promptfiles as pf
            sub, _, name = arg.partition(" ")
            name = name.strip().removesuffix(".md")
            if sub == "edit" and name:
                try:
                    path = pf.edit(name)
                    self._line(f"Edit {path} in any editor, then /prompts reload. The comment at the top lists its "
                               "placeholders; /prompts reset " + name + " goes back to the default.", "ok")
                except KeyError:
                    self._line(f"No prompt called {name!r}. /prompts lists them.", "error")
            elif sub == "reset" and name:
                names = [n for n, src, _ in pf.listing() if src != "default"] if name == "all" else [name]
                done = [n for n in names if pf.reset(n)]
                self._line(("Back to the default for: " + ", ".join(done) + ". Your versions are kept as .md.off "
                            "files, and /prompts undo <name> brings one back.") if done else "Nothing to reset.", "ok")
                self._reload_prompts()
            elif sub == "undo" and name:
                when = pf.undo(name)
                self._line(f"{name}: back to your version from {when}." if when else
                           f"No earlier version of your {name}.md is saved.", "ok" if when else "error")
                self._reload_prompts()
            elif sub == "style":
                style = name.strip()
                if style not in pf.STYLES:
                    self._line(f"Prompt style: {pf.STYLE}. /prompts style compact | hermes switches it for this "
                               "session; prompts.style in config.yaml keeps it.", "help")
                else:
                    pf.set_style(style)
                    self._reload_prompts()
                    self._line(f"Prompt style: {style}, from the next message on. To keep it, set prompts.style: "
                               f"{style} in ~/.hermes-lite/config.yaml (or run hermes-lite --hermes-prompts).", "ok")
            elif sub == "reload":
                self._reload_prompts()
                self._line("Prompts reloaded. The model gets them from the next message on (the system prompt "
                           "is read once more).", "ok")
            else:
                rows = pf.listing()
                width = max(len(n) for n, _, _ in rows)
                mark = {"yours": "✎", "ignored": "⚠", "default": " "}
                text = "\n".join(f"{mark[src]} {n:<{width}}  {about}" for n, src, about in rows)
                state = "" if pf.ENABLED else "\n(Your versions are switched OFF: prompts.overrides: false or --default-prompts.)"
                self._line(f"Prompt files (✎ your version in {pf.user_dir()}, ⚠ your copy is ignored):\n{text}{state}\n\n"
                           "/prompts edit <name> · /prompts reset <name>|all · /prompts undo <name> · /prompts reload",
                           "help")
            shown = self.__dict__.setdefault("_prompt_warnings_shown", set())
            for w in pf.WARNINGS:
                if w not in shown:
                    shown.add(w)
                    self._line("⚠ " + w, "error")
        elif cmd == "notes":
            from .. import projectnotes
            path = projectnotes.notes_path(self.cfg, self.agent.ctx.cwd)
            if path is None:
                self._line("No project notes here: they are kept for projects (a Git repository or a folder with a "
                           "project file), and agent.project_notes must be on.", "help")
            elif arg.strip() in ("update", "rewrite"):
                how = "rewrite" if arg.strip() == "rewrite" else "manual"
                if self.agent.busy:
                    self._line("Wait for the current turn to finish, or press Esc first.", "error")
                else:
                    async def go():
                        if not self.server_ready.is_set():
                            await self.server_ready.wait()
                        await self.agent.notes_now(how)
                        self._ev_idle()
                    self.run_worker(go(), group="agent")
            else:
                text = projectnotes.load(path)
                self._line((f"{path}\n\n{text}" if text else f"No notes yet ({path}). They are written after a stretch "
                            "of work, when the session ends, or now with /notes update."), "help")
        elif cmd == "learn":
            if self.agent.busy:
                self._line("Wait for the current turn to finish, or press Esc first.", "error")
            else:
                async def go():
                    if not self.server_ready.is_set():
                        await self.server_ready.wait()
                    await self.agent.learn()
                self.run_worker(go(), group="agent")
        elif cmd == "stream":
            llm = self.agent.llm
            import time as _t
            quiet = _t.monotonic() - llm.last_data if llm.last_data else None
            mode = {True: "Hermes Lite parses tool calls (streamed)", False: "llama-server parses tool calls",
                    None: "tool parsing not checked yet"}[llm.client_tools]
            self._line(f"Current reply — received: thinking {llm.rx['thinking']} chars · text {llm.rx['text']} chars · "
                       f"tool-call arguments {llm.rx['tool']} chars"
                       + (f" · last data {quiet:.0f} s ago" if quiet is not None else "")
                       + f"\n{mode} · full log: {llm.stream_log}\n\n"
                       + (llm.rx_tail[-2500:] or "(nothing received yet)"), "help")
        elif cmd == "export":
            path = self._export(arg)
            self._line(f"Conversation saved to {path} — open it in any editor to copy from it.", "ok")
        elif cmd == "copy":
            last = next((m.get("content") for m in reversed(self.agent.messages)
                         if m["role"] == "assistant" and (m.get("content") or "").strip()), "")
            text = self.last_error if arg == "error" or (self.last_error and not last) else last
            if arg == "error":
                text = self.last_error
            self._copy(text) if text else self._line("Nothing to copy yet.")
        elif cmd == "retry":
            self._retry()
        elif cmd == "undo":
            self._undo()
        elif cmd in ("compress", "compact"):
            if self.agent.busy:
                self._line("Wait for the current turn to finish, or press Esc first.", "error")
            else:
                async def go():  # in a worker: the input, Esc and other commands keep working meanwhile
                    self.last_error, self._compaction_cancelled = "", False
                    done = await self.agent.compact_now()
                    self._ev_idle()
                    if not done and not self.last_error_recent() and not self._compaction_cancelled:
                        self._line("Nothing compacted (nothing to compact yet, or it was cancelled).")
                self.run_worker(go(), group="agent")
        elif cmd == "usage":
            used, total = self.agent.context_usage()
            self._line(f"Context {used:,} / {total:,} tokens ({100 * used / total:.0f}%) · last speed "
                       f"{self.agent.last_tok_s:.1f} tok/s · {len(self.agent.messages)} messages")
        elif cmd == "cwd":
            path = Path(arg or "~").expanduser().resolve()
            if not path.is_dir():
                self._line(f"Not a directory: {path}", "error")
            else:
                self.agent.ctx.cwd = self.agent.ctx.state["start_cwd"] = str(path)
                from ..prompts import build_system_prompt
                self.agent.messages[0]["content"] = build_system_prompt(self.cfg, self.agent.ctx, self.agent.memory)
                self._line(f"Working directory: {path}", "ok")
                self._refresh_header()
        elif cmd in ("think", "thinking"):
            on = arg.lower() not in ("off", "false", "0", "no") if arg else not self.cfg["model"].get("thinking", True)
            self.cfg["model"]["thinking"] = on
            self._line(f"Model thinking {'on' if on else 'off'}.", "ok")
        elif cmd in ("forget-password", "forgetpw"):
            from ..tools import askpass
            askpass.forget(self.agent.ctx)
            self._line("Forgot the remembered sudo password.", "ok")
        elif cmd == "yolo":
            self.action_toggle_yolo()
        elif cmd == "memory":
            m = self.agent.memory
            self._line((m.block("memory") or "Memory is empty.") + "\n\n" + (m.block("user") or "User profile is empty."), "help")
        elif cmd == "skills" and arg.lower() in ("browse", "catalog", "catalogue", "add"):
            self._browse_skills()
        elif cmd == "skills":
            cat = catalog(self.agent.ctx)
            self._line(("\n".join(f"{n}  —  {i['description']}" for n, i in sorted(cat.items()))
                        or f"No skills yet. They live in {self.agent.home / 'skills'}.")
                       + "\n\n/skills browse: pick skills from a catalogue (Hermes Agent's, or skills.catalog).", "help")
        elif cmd == "tools":
            from ..tools.base import disabled_tools
            off = disabled_tools(self.cfg)
            rows = []
            for t in REGISTRY.values():
                state = ("off (agent.disabled_tools)" if t.name in off else
                         "off (no vision encoder)" if t.requires == "vision" and not self.agent.llm.vision else "on")
                rows.append(f"{t.emoji} {t.name:16} {state}")
            self._line("\n".join(rows) + "\n\nTurn tools off in config.yaml: agent.disabled_tools: [github, read_docs]"
                       " — they are then left out of the prompt entirely.", "help")
        elif cmd == "vision":
            await self._vision(arg.lower())
        elif cmd == "telegram":
            # a worker: it may open the token box, which needs this input handler to be free
            self.run_worker(self._telegram_command(arg), group="telegram")
        elif cmd == "server" and arg in ("handoff", "give", "release"):
            async def hand():
                if self.agent.busy:
                    self.agent.interrupt()
                    await asyncio.sleep(0.3)
                self.server_ready.clear()
                self._hint("Stopping Hermes Lite's llama-server…")
                started = await self.server.hand_over()
                self._refresh_header()
                self._line("GPU handed over: Hermes Lite's llama-server is stopped"
                           + ("; started again: " + ", ".join(started) if started else
                              " (no other server was waiting to be started again)")
                           + ". Your next message here takes the GPU back.", "ok")
            self.run_worker(hand(), group="server")
        elif cmd == "server":
            if arg in ("restart", "reset"):
                if arg == "reset":  # forget the saved fallback setting and start from config.yaml again
                    self.server.rung, self.server.trusted = 0, False
                    self.server.tuning_file.unlink(missing_ok=True)
                if self.agent.busy:  # a request in flight would keep the old server busy
                    self.agent.interrupt()
                info = None
                if arg == "restart" and not self.server.owned and \
                        str(self.cfg.get_path("server.restart_external", "same")) == "same":
                    info = self.server.external_info()
                    from ..server import MIN_CONTEXT, flag_table
                    flags = flag_table(info["argv"]) if info and info.get("argv") else {}
                    small = next((flags[f] for f in ("-c", "--ctx-size") if f in flags), None)
                    if small and small.isdigit() and 0 < int(small) < MIN_CONTEXT:
                        self._line(f"That server has only {int(small):,} tokens of context, too small for Hermes Lite: "
                                   "replacing it with Hermes Lite's settings on the same port instead.", "error")
                        info = None
                self.server_ready.clear()
                if info and info.get("argv"):
                    def report(state: str, detail: str = "") -> None:
                        self._refresh_header()
                        if state == "error":
                            self._line(f"llama-server: {detail}\n/server reset starts Hermes Lite's own instead.", "error")
                        elif detail:
                            self._hint(detail)

                    async def again():
                        if await self.server.restart_external(info, report):
                            self._line("llama-server restarted with its own settings.", "ok")
                            await self._boot_server()
                    self._line(f"Force-restarting the llama-server on port {self.cfg['server'].get('port')} "
                               f"(PID {info['pid']}) with its own settings…")
                    self.run_worker(again(), group="server")
                else:
                    async def fresh():
                        self._hint("Stopping llama-server…")
                        killed = await self.server.stop_any()   # also a server Hermes Lite did not start
                        self._line("Restarting llama-server…" + (f" (stopped process {', '.join(map(str, killed))})"
                                                                 if killed else ""))
                        await self._boot_server()
                    self.run_worker(fresh(), group="server")
            else:
                running = await self.server.served_model()
                if running:
                    self._line(f"Model loaded by the server: {running}", "help")
                info = None if self.server.owned else self.server.external_info()
                if info and info.get("argv"):
                    from ..server import compare_commands
                    from ..oscompat import process_cmdline
                    parent = ""
                    try:
                        ppid = int(Path(f"/proc/{info['pid']}/stat").read_text().rsplit(")", 1)[1].split()[1])
                        parent = process_cmdline(ppid)[:80]
                    except (OSError, ValueError, IndexError):
                        pass
                    diff = compare_commands(info["argv"], build_command(self.cfg, self.server.rung))
                    rows = "\n".join(f"  {k:22} {a:34} {b}" for k, a, b in diff) or "  (none)"
                    self._line(f"llama-server at {self.server.base}: not started by Hermes Lite (PID {info['pid']}"
                               + (f", started by: {parent}" if parent else "") + ")\n$ " + " ".join(info["argv"])
                               + f"\n\nDifferences from Hermes Lite's own settings:\n  {'flag':22} {'running':34} Hermes Lite\n"
                               + rows + "\n\n/server restart force-restarts it with ITS settings "
                               "(server.restart_external: own = with Hermes Lite's).", "help")
                self._line(f"llama-server {self.server.status} at {self.server.base}"
                           f" ({'started by Hermes Lite' if self.server.owned else 'external'})\n$ "
                           + " ".join(build_command(self.cfg, self.server.rung)) + f"\n(setting: {self.server.rung_label()}"
                           + ("; Hermes Lite's own command, used when it starts the server" if info else "") + ")"
                           + "\nTool calls: " + {True: "parsed by Hermes Lite (streamed live, no grammar)",
                                                 False: "parsed by llama-server", None: "not checked yet"}[self.agent.llm.client_tools]
                           + "\n\n" + (self.server.log_tail(12) or ""), "help")
        else:
            self._line(f"Unknown command /{cmd}. Type /help.", "error")

    def _export(self, arg: str = "") -> str:
        import json as _json
        import time as _time
        out = [f"# {self.agent.title or 'Hermes Lite session'}", ""]
        for m in self.agent.messages[1:]:
            role, content = m["role"], (m.get("content") or "").strip()
            if role == "user" and not m.get("_synthetic"):
                out += ["## You", "", content, ""]
            elif role == "assistant":
                for tc in m.get("tool_calls") or []:
                    out += [f"> tool: {tc['function']['name']} {tc['function']['arguments'][:300]}", ""]
                if content:
                    out += ["## Hermes", "", content, ""]
            elif role == "tool":
                try:
                    d = _json.loads(content)
                    body = d.get("output") or d.get("content") or d.get("error") or content
                except ValueError:
                    body = content
                out += ["```", str(body)[:4000], "```", ""]
        target = Path(arg).expanduser() if arg else (
            Path(self.agent.ctx.cwd) / f"hermes-lite-{_time.strftime('%Y%m%d-%H%M%S')}.md")
        target.write_text("\n".join(out), encoding="utf-8")
        return str(target)

    def _last_user_index(self) -> int:
        return max((i for i, m in enumerate(self.agent.messages) if m["role"] == "user" and not m.get("_synthetic")),
                   default=-1)

    def _retry(self) -> None:
        if self.agent.busy:
            return
        i = self._last_user_index()
        if i < 0:
            self._line("Nothing to retry.")
            return
        text = self.agent.messages[i]["content"]
        del self.agent.messages[i:]
        self._mount(UserMessage(text))
        self._start_run(text)

    def _undo(self) -> None:
        if self.agent.busy:
            return
        i = self._last_user_index()
        if i < 0:
            self._line("Nothing to undo.")
            return
        del self.agent.messages[i:]
        self.agent.save()
        self._line("Removed your last message and everything after it from the conversation.", "ok")

    def _load(self, data: dict) -> None:
        self.agent.load(data)
        self.log_view.remove_children()
        for m in self.agent.messages[1:]:
            role = m["role"]
            if role == "user":
                if m.get("_compaction"):
                    self._mount(SystemLine("Earlier context was compacted into a checkpoint summary.", "ok"))
                elif not m.get("_synthetic"):
                    self._mount(UserMessage(m["content"]))
            elif role == "assistant":
                for tc in m.get("tool_calls") or []:
                    name = tc["function"]["name"]
                    tool = REGISTRY.get(name)
                    try:
                        label = tool.label(json.loads(tc["function"]["arguments"])) if tool else ""
                    except (ValueError, TypeError, AttributeError):
                        label = ""
                    card = ToolCard(tc["id"], name, label, tool.emoji if tool else "?")
                    self.cards[tc["id"]] = card
                    self._mount(card)
                if (m.get("content") or "").strip():
                    md = AssistantMessage()
                    md.add(m["content"])
                    self._mount(md)
                    self.call_after_refresh(lambda md=md: self.run_worker(md.flush()))
            elif role == "tool" and (card := self.cards.pop(m.get("tool_call_id", ""), None)):
                card.finish(m["content"], '"success": false' not in m["content"][:40], 0.0)
        self.cards.clear()
        self.query_one(Sidebar).show_todos(self.agent.ctx.state.get("todos", []))
        self._line(f"Resumed “{self.agent.title}”.", "ok")
        self.agent.last_prompt_tokens = self.agent._estimate()
        self._refresh_header()
        self._refresh_sidebar()
        self.set_timer(0.3, lambda: self.log_view.scroll_end(animate=False))

    # ── actions ─────────────────────────────────────────────────────────
    def action_interrupt(self) -> None:
        if self.agent.busy:
            self.agent.interrupt()
            self._hint("Interrupting…")

    def _copy(self, text: str) -> None:
        """Copy through every route: a system clipboard tool (verified by reading it back), the
        terminal's OSC 52, and a file, so the text is never lost. A pop-up says what happened."""
        from . import clipboard
        from ..config import home as _home
        saved = _home() / "clipboard.txt"
        try:
            saved.write_text(text, encoding="utf-8")
            saved.chmod(0o600)
        except OSError:
            pass
        tool, reason = clipboard.copy(text)
        self.copy_to_clipboard(text)  # OSC 52 through the terminal, as well
        if tool:
            self.notify(f"Copied {len(text)} characters. Paste with Ctrl+V (Ctrl+Shift+V in a terminal).",
                        title="Copied", timeout=3)
            self._hint(f"Copied {len(text)} characters ✓")
        else:
            self.notify(f"The system clipboard did not take it ({reason}). The text is saved in {saved} "
                        "and was also sent through the terminal.", title="Copy", severity="warning", timeout=10)
            self._hint(f"Copy not verified · text saved to {saved}")

    def on_mouse_up(self, event) -> None:
        """Copy on select: releasing the mouse after selecting text copies it."""
        def grab() -> None:
            try:
                text = self.screen.get_selected_text()
            except Exception:
                return
            if text and text.strip() and text != self._last_auto_copy:
                self._last_auto_copy = text
                self._copy(text)
        self.call_after_refresh(grab)

    async def action_ctrl_c(self) -> None:
        selected = self.screen.get_selected_text()
        if selected:
            self._copy(selected)
            self.screen.clear_selection()
            return
        if self.agent.busy:
            self.action_interrupt()
            return
        if self._ctrl_c_armed:
            await self.action_quit()
            return
        self._ctrl_c_armed = True
        self._hint("Press Ctrl+C again to quit.")
        self.set_timer(2.0, lambda: setattr(self, "_ctrl_c_armed", False))

    def _reload_prompts(self) -> None:
        from .. import promptfiles as pf
        from ..prompts import build_system_prompt
        pf.reload()
        if self.agent.messages and self.agent.messages[0].get("role") == "system":
            self.agent.messages[0]["content"] = build_system_prompt(self.cfg, self.agent.ctx, self.agent.memory)
        if hasattr(self.agent, "_schema_tokens"):
            del self.agent._schema_tokens

    async def action_new_session(self) -> None:
        if self.agent.busy:
            self.agent.interrupt()
            await asyncio.sleep(0.3)
        if self.agent.notes_pending() and self.server_ready.is_set() and \
                self.cfg.get_path("agent.project_notes_on_exit", False):
            await self.agent.notes_now("session end")
        old_cwd = self.agent.ctx.cwd
        await self.agent.shutdown()
        self.agent = Agent(self.cfg, self, cwd=old_cwd)
        self.log_view.remove_children()
        self.cards.clear()
        self.query_one(Sidebar).show_todos([])
        self._line("New session.", "ok")
        self._announce_carryover()
        self._refresh_header()
        self._refresh_sidebar()

    async def action_resume(self) -> None:
        def chosen(sid: str | None) -> None:
            if sid and (data := load_session(sid)):
                self._load(data)
        self.push_screen(SessionScreen(list_sessions()), chosen)

    def action_toggle_sidebar(self) -> None:
        self.query_one(Sidebar).toggle_class("hidden")

    def action_toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        for block in self.query(ThinkingBlock):
            block.display = self.show_thinking
        self._hint(f"Thinking blocks {'shown' if self.show_thinking else 'hidden'}.")

    def action_toggle_yolo(self) -> None:
        mode = "off" if self.cfg.get_path("agent.approvals", "smart") != "off" else "smart"
        self.cfg["agent"]["approvals"] = mode
        self._line("Command approvals off (yolo): dangerous commands run without asking." if mode == "off"
                   else "Command approvals on: Hermes asks before dangerous commands.", "error" if mode == "off" else "ok")
        self._refresh_header()

    async def action_quit(self) -> None:
        if self.telegram:
            await self.telegram.stop("⚪ Hermes Lite quit.")
            self.telegram = None
        if getattr(self, "memguard", None):
            self.memguard.stop()
        if self.agent.busy:
            self.agent.interrupt()
            await asyncio.sleep(0.2)
        if self.agent.notes_pending() and self.server_ready.is_set() and \
                self.cfg.get_path("agent.project_notes_on_exit", False) and not getattr(self, "_quitting", False):
            self._quitting = True  # a second Ctrl+Q quits at once
            self._line("Updating the project notes before quitting… (Esc skips, Ctrl+Q again quits now)")
            await self.agent.notes_now("session end")
        await self.agent.shutdown()
        if self.cfg.get_path("server.stop_on_exit", True):
            pid = self.server.pid()
            self.server.stop()
            if self.server.stopped() and self.cfg.get_path("server.restore_on_exit", True):
                from .. import oscompat
                for _ in range(80):  # let ours free the GPU before the other one loads
                    if not pid or not oscompat.pid_alive(pid):
                        break
                    await asyncio.sleep(0.1)
                if pid and oscompat.pid_alive(pid):
                    oscompat.force_kill(pid)
                    await asyncio.sleep(0.5)
                self.server.give_back()
        self.exit()
