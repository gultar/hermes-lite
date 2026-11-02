"""The Hermes conversation loop: stream → run tools → feed results back, until the model answers."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import re
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Protocol

from . import compaction
from .config import Config, home
from . import loopguard, planmode, projectnotes
from .worklog import WorkLog
from . import promptfiles as pf
from .llm import LLM, LLMError, ToolCall, Turn
from .prompts import STEER_CLOSE, STEER_OPEN, build_system_prompt
from .tools import REGISTRY, ToolContext
from .tools.base import disabled_tools, in_trash, schema_without
from .tools.memory import MemoryStore
from .tools.terminal import kill_all

REVIEW_TOOLS = ("skills_list", "skill_view", "skill_manage")

class UI(Protocol):
    """What the agent needs from the interface. Event methods are called on the event loop."""
    def event(self, kind: str, **data: Any) -> None: ...
    async def approve(self, command: str, reason: str) -> str: ...
    async def clarify(self, questions: list) -> list: ...


_IMAGE_CACHE: dict[str, str] = {}


def _data_url(path: str) -> str | None:
    """data: URL for an image file (cached; images are attached once, so this stays small)."""
    if path in _IMAGE_CACHE:
        return _IMAGE_CACHE[path]
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    import base64
    ext = Path(path).suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "tif": "image/tiff"}.get(ext, f"image/{ext or 'png'}")
    url = f"data:{mime};base64," + base64.b64encode(raw).decode()
    if len(_IMAGE_CACHE) > 8:
        _IMAGE_CACHE.clear()
    _IMAGE_CACHE[path] = url
    return url


class Agent:
    def __init__(self, cfg: Config, ui: UI, cwd: Optional[str] = None, session_id: Optional[str] = None):
        self.cfg, self.ui = cfg, ui
        self.home = home()
        self.llm = LLM(cfg)
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.memory = MemoryStore(self.home)
        _prune_scratch(self.home / "scratch")
        try:
            self.loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self.ctx = ToolContext(cfg=cfg, home=self.home, cwd=os.path.abspath(cwd or os.getcwd()),
                               session_id=self.session_id)
        self.ctx.state["memory_store"] = self.memory
        self.ctx.state["cancel"] = threading.Event()
        self.ctx.state["start_cwd"] = self.ctx.cwd  # where a working folder that went to the Trash falls back to
        self.ctx.approve = self._approve_from_thread
        self.ctx.clarify = self._clarify_from_thread
        self.ctx.ask_secret = self._secret_from_thread
        self.ctx.notify = self._notify_from_thread
        from .tools import notes as _notes
        if (left := _notes.unfinished_todos(self.ctx.cwd)):  # the project's todo list from the last session
            self.ctx.state["todos"], self.ctx.state["todos_restored"] = left, True
        self.messages: list[dict] = [{"role": "system", "content": build_system_prompt(cfg, self.ctx, self.memory)}]
        self.title = ""
        self.busy = False
        self.steer_queue: list[str] = []
        self.notifications: list[str] = []
        self.last_prompt_tokens = 0
        self._measured: Optional[tuple] = None  # (prompt tokens llama-server counted, messages sent, last one)
        self.worklog = WorkLog()                # facts every compaction handoff carries word for word
        self._active_request = ""              # the request in progress, once compaction removed it
        self.last_tok_s = 0.0
        self._stream_task: Optional[asyncio.Task] = None
        self._cancel = asyncio.Event()
        self._summary = ""
        self.mode = "normal"        # "plan" while /plan is on: look, don't touch
        self.plan_task = ""         # what the plan is for
        self.plan_text = ""         # the plan being built (re-attached at compaction)
        self.plan_written = ""      # the plan exactly as the model wrote it (to spot user edits)
        self.force_plan = False     # next run: no tools, write the plan
        self.iters_since_review = 0
        self.iters_since_notes = 0   # tool calls since the project notes were last updated
        self.reviewing = False

    # ── thread → loop bridges for tools ────────────────────────────────
    def _approve_from_thread(self, command: str, reason: str) -> str:
        assert self.loop
        return asyncio.run_coroutine_threadsafe(self.ui.approve(command, reason), self.loop).result()

    def _clarify_from_thread(self, questions: list) -> list:
        assert self.loop
        return asyncio.run_coroutine_threadsafe(self.ui.clarify(questions), self.loop).result()

    def _secret_from_thread(self, prompt: str, command: str) -> tuple:
        if not self.loop or not hasattr(self.ui, "ask_secret"):
            return None, False
        return asyncio.run_coroutine_threadsafe(self.ui.ask_secret(prompt, command), self.loop).result()

    def _notify_from_thread(self, text: str) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._notify, text)

    def _notify(self, text: str) -> None:
        self.notifications.append(text)
        self.ui.event("notification", text=text, idle=not self.busy)

    # ── public controls ─────────────────────────────────────────────────
    def interrupt(self) -> None:
        self._cancel.set()
        self.ctx.state["cancel"].set()
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()

    def steer(self, text: str) -> None:
        self.steer_queue.append(text)

    def tools_schema(self) -> list[dict]:
        off = disabled_tools(self.cfg)
        return [schema_without(t, off) for t in REGISTRY.values()
                if t.name not in off and not (t.requires == "vision" and not self.llm.vision)]

    def context_usage(self) -> tuple[int, int]:
        return self._context_tokens(), int(self.cfg.get_path("model.context_length", 64000))

    async def shutdown(self) -> None:
        kill_all(self.ctx)
        await self.llm.aclose()
        import shutil  # GitHub files kept for this session only
        shutil.rmtree(self.home / "scratch" / self.session_id / "github", ignore_errors=True)

    # ── message plumbing ────────────────────────────────────────────────
    def _wire_messages(self) -> list[dict]:
        """Strip private keys and, by default, past reasoning.

        Qwen's chat template only renders reasoning for the turn after the latest user message, so
        sending it back makes every earlier turn re-render differently once you type again. That
        breaks llama-server's prompt cache and forces it to re-read the whole previous turn, which
        on a CPU-heavy setup costs minutes. Qwen's own guidance is to keep only final outputs in
        history, so reasoning is not replayed unless model.replay_reasoning is true."""
        replay = bool(self.cfg.get_path("model.replay_reasoning", False))
        last_user = max((i for i, m in enumerate(self.messages)
                         if m["role"] == "user" and not m.get("_synthetic")), default=0)
        out = []
        for i, m in enumerate(self.messages):
            w = {k: v for k, v in m.items() if not k.startswith("_")}
            if w.get("role") == "assistant" and (not replay or i < last_user):
                w.pop("reasoning_content", None)
            if m.get("_images") and self.llm.vision:
                parts = [{"type": "text", "text": w["content"]}]
                for path in m["_images"]:
                    if url := _data_url(path):
                        parts.append({"type": "image_url", "image_url": {"url": url}})
                w["content"] = parts if len(parts) > 1 else w["content"]
            out.append(w)
        return out

    def _attach_pending_images(self) -> None:
        """Images from this round's screenshot/view_image calls: one message, shown to the model once."""
        pending = self.ctx.state.pop("pending_images", None)
        if not pending:
            return
        labels = "; ".join(p["label"] for p in pending)
        asks = " ".join(f"Look for: {p['question']}" for p in pending if p.get("question"))
        text = (f"[Hermes Lite attached {len(pending)} image(s) from your tool calls: {labels}. You see "
                f"{'it' if len(pending) == 1 else 'them'} only in this step: start your reply by writing down "
                "everything that matters for the task (visible text, values, layout, errors, what is missing) — "
                f"afterwards the image is replaced by your notes.{(' ' + asks) if asks else ''}]")
        self.messages.append({"role": "user", "content": text, "_synthetic": True,
                              "_images": [p["path"] for p in pending], "_image_labels": labels})
        self.ui.event("images_attached", labels=labels, count=len(pending))

    def _retire_images(self, turn: Turn) -> None:
        """After the model replied to an image: replace it with the model's own notes on it (its visible
        reply, or its thinking when it only called tools — thinking is not sent back later)."""
        for m in self.messages:
            if m.get("_images"):
                notes = (turn.content or "").strip() or (turn.reasoning or "").strip()
                if len(notes) > 2500:
                    notes = notes[:2500] + " […]"
                m["content"] = (f"[Image(s) shown here earlier and since removed to save context: "
                                f"{m.get('_image_labels', '')}. Your notes when you saw "
                                f"{'it' if len(m['_images']) == 1 else 'them'}:\n{notes or '(none written)'}\n"
                                "To look again, take a new screenshot or call view_image again.]")
                m.pop("_images", None)

    def _estimate(self, messages: Optional[list] = None, schema: bool = True) -> int:
        """Estimated prompt size of *messages* (default: the whole conversation), as it is sent: past
        thinking is not sent back, so it is not counted (unless model.replay_reasoning)."""
        if not hasattr(self, "_schema_tokens"):
            self._schema_tokens = int(len(json.dumps(self.tools_schema())) / compaction.CHARS_PER_TOKEN)
        messages = self.messages if messages is None else messages
        images = sum(len(m.get("_images") or []) for m in messages)
        replay = bool(self.cfg.get_path("model.replay_reasoning", False))
        return compaction.estimate_tokens(messages, reasoning=replay) + (self._schema_tokens if schema else 0) + \
            images * int(self.cfg.get_path("server.image_max_tokens", 1024) or 1024)

    def _context_tokens(self) -> int:
        """How full the context will be on the next request: llama-server's own count of the last
        request, plus an estimate of what was added since. (The reply's thinking is not in it: it is
        not sent back.)"""
        m = self._measured
        if m and m[1] <= len(self.messages) and m[1] > 0 and self.messages[m[1] - 1] is m[2]:
            return m[0] + self._estimate(self.messages[m[1]:], schema=False)
        return self._estimate()

    def _limits(self, ctx_len: int) -> tuple[int, int]:
        """(compact above this many prompt tokens, recent tokens kept word for word when compacting).

        The prompt must leave room for the next reply — its thinking (up to the loop guard's budget)
        plus the answer or tool call — so the limit is the smaller of compress_at × context and
        context − that reserve. The verbatim tail is sized so a compaction frees real room."""
        share = float(self.cfg.get_path("agent.compress_at", 0.8) or 0.8)
        reserve = self.cfg.get_path("agent.reply_reserve", "auto")
        if reserve in (None, "auto"):
            budget = int(loopguard.settings(self.cfg).get("thinking_budget") or 0) or 6144
            reserve = min(budget + 2048, int(ctx_len * 0.35))
        limit = int(min(share * ctx_len, ctx_len - int(reserve)))
        keep = self.cfg.get_path("agent.keep_recent_tokens", "auto")
        fixed = self._estimate([self.messages[0]])  # system prompt + tool list
        room = max(2000, limit - fixed - 3500)      # 3500: the summary and the facts list
        keep = int(ctx_len * 0.25) if keep in (None, "auto") else int(keep)
        return limit, max(1500, min(keep, int(room * 0.5)))

    def save(self) -> None:
        data = {"id": self.session_id, "title": self.title, "cwd": self.ctx.cwd, "updated": time.time(),
                "messages": self.messages, "todos": self.ctx.state.get("todos", []), "summary": self._summary,
                "mode": self.mode, "plan_task": self.plan_task, "plan_text": self.plan_text,
                "plan_written": self.plan_written, "worklog": self.worklog.to_dict(),
                "active_request": self._active_request, "iters_since_notes": self.iters_since_notes}
        path = self.home / "sessions" / f"{self.session_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def load(self, data: dict) -> None:
        self.session_id = data["id"]
        self.ctx.session_id = data["id"]
        self.title = data.get("title", "")
        # The folder you launched Hermes Lite in wins over the one the session was saved in: projects get
        # moved, renamed or replaced. Only when launched from the home folder (no project) does a
        # resumed session go back to its own folder, if that still exists and is not in the Trash.
        launched = self.ctx.state.get("start_cwd") or self.ctx.cwd
        saved = data.get("cwd") or launched
        if Path(launched).resolve() == Path.home().resolve() and os.path.isdir(saved) and not in_trash(saved):
            launched = saved
        self.ctx.cwd = self.ctx.state["start_cwd"] = launched
        self.messages = data["messages"]
        self.messages[0] = {"role": "system", "content": build_system_prompt(self.cfg, self.ctx, self.memory)}
        self.ctx.state["todos"] = data.get("todos", [])
        self._summary = data.get("summary", "")
        self.mode = data.get("mode", "normal")
        self.plan_task = data.get("plan_task", "")
        self.plan_text = data.get("plan_text", "")
        self.plan_written = data.get("plan_written", "")
        self.worklog = WorkLog.from_dict(data.get("worklog"))
        self._active_request = data.get("active_request", "")
        self.iters_since_notes = int(data.get("iters_since_notes", 0) or 0)
        self._measured = None

    # ── plan / build ────────────────────────────────────────────────────
    def start_plan(self) -> None:
        self.mode, self.plan_task = "plan", ""

    def stop_plan(self) -> None:
        self.mode = "normal"

    def build_message(self) -> str | None:
        """Leave plan mode and return the build instruction (None when there is no plan)."""
        plan = planmode.load_plan(self.ctx.cwd)
        if not plan:
            return None
        self.mode, self.plan_text = "normal", plan
        edited = " (the user edited it after you wrote it: follow this version)" \
            if self.plan_written and plan.strip() != self.plan_written.strip() else ""
        items = planmode.steps(plan)
        if items:
            self.ctx.state["todos"] = [{"id": str(i + 1), "content": t, "status": "in_progress" if i == 0 else "pending"}
                                       for i, t in enumerate(items)]
            self.ui.event("todos", todos=list(self.ctx.state["todos"]))
            from .tools import notes as _notes
            _notes.save_todos(self.ctx.cwd, self.ctx.state["todos"])
        return pf.get("build", plan=plan, edited=edited)

    # ── compaction ──────────────────────────────────────────────────────
    def _read_results(self) -> list[tuple[int, tuple, int]]:
        """(index, (path, offset, limit, outline_only), characters) of every read_file result that
        still holds file content, newest first. "unchanged" replies hold none: they point back to the
        earlier result, which therefore counts as the latest read."""
        args = {tc.get("id"): (tc.get("function") or {}).get("arguments")
                for m in self.messages for tc in m.get("tool_calls") or []}
        out = []
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m.get("role") != "tool" or m.get("name") != "read_file" or m.get("_pruned"):
                continue
            try:
                d = json.loads(m.get("content") or "")
                a = json.loads(args.get(m.get("tool_call_id")) or "{}")
                key = (d["path"], int(a.get("offset") or 1), a.get("limit"), bool(a.get("outline_only")))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            if isinstance(d, dict) and ("content" in d or "outline" in d):
                out.append((i, key, len(m.get("content") or "")))
        return out

    def _protected_reads(self, budget: int) -> tuple[set, set]:
        """(results never trimmed, results trimmed even when recent). The latest read_file results stay
        whole — up to agent.keep_recent_reads of them, within *budget* tokens, and always the newest
        one. An earlier read of the same file and range is superseded by the later one."""
        n = int(self.cfg.get_path("agent.keep_recent_reads", 3) or 0)
        protect, superseded, seen, used = set(), set(), set(), 0
        for i, key, chars in self._read_results():
            if key in seen:
                superseded.add(i)
                continue
            seen.add(key)
            tokens = int(chars / compaction.CHARS_PER_TOKEN)
            if len(protect) < n and (not protect or used + tokens <= budget):
                protect.add(i)
                used += tokens
        return protect, superseded

    def _reads_budget(self, limit: int) -> int:
        return max(2000, int((limit - self._estimate([self.messages[0]])) * 0.45))

    def _prune(self, keep: int, limit: int) -> tuple[int, int]:
        """Trim big tool outputs (and the file bodies inside write_file/patch calls) older than the
        recent *keep* tokens — except the latest read_file results, which stay whole — and earlier
        reads of a file that was read again since. The conversation itself stays word for word."""
        start = compaction.split_point(self.messages, keep)
        protect, superseded = self._protected_reads(self._reads_budget(limit))
        items, freed = compaction.prune(self.messages, 1, start, protect=protect, extra=superseded)
        if items:
            self._measured = None
        return items, freed

    def _tail_with_reads(self, start: int, limit: int) -> int:
        """Move the verbatim tail's start back so the latest read_file results are in it, when that
        still leaves a compaction worth doing; else keep fewer of them (at least try the newest)."""
        protect, _ = self._protected_reads(self._reads_budget(limit))
        room = max(2000, limit - self._estimate([self.messages[0]]) - 3500)
        for idx in sorted(protect):  # oldest first: drop the oldest protected read when it does not fit
            if idx >= start:
                return start
            call_id = self.messages[idx].get("tool_call_id")
            a = next((j for j in range(idx - 1, 0, -1) if any(tc.get("id") == call_id
                      for tc in self.messages[j].get("tool_calls") or [])), None)
            if a is None or a < 3:
                continue
            if self._estimate(self.messages[a:], schema=False) <= room * 0.75:
                return a
        return start

    async def compact_now(self) -> bool:
        """/compress: compact outside a turn — shown as busy (progress line, Esc cancels)."""
        self.loop = asyncio.get_running_loop()
        self.busy = True
        self._cancel.clear()
        try:
            return await self.compact("manual")
        finally:
            self.busy = False
            self._cancel.clear()
            self.ctx.state["cancel"].clear()

    async def _write_summary(self, middle: list[dict], ctx_len: int) -> Optional[str]:
        """The checkpoint summary, or None (failed or cancelled: nothing changes).

        Preferred: ask for it at the end of the conversation as it is now. llama-server has all of
        that cached, so only the request and the summary itself cost time. When the conversation
        is too full for that (an overflow), the older turns are sent as a new, size-capped prompt."""
        saved = self.cfg["model"].get("thinking", True)
        self.cfg["model"]["thinking"] = False
        written = [0]

        def on_content(text: str) -> None:
            written[0] += len(text)
            if written[0] // 300 != (written[0] - len(text)) // 300:
                self.ui.event("status", text=f"compacting context… writing the summary "
                              f"(~{int(written[0] / compaction.CHARS_PER_TOKEN)} tokens) · Esc cancels")

        in_place = self._context_tokens() + 4000 <= ctx_len
        if in_place:
            today = dt.date.today().strftime("%A, %B %d, %Y")
            convo = self._wire_messages() + [{"role": "user", "content": pf.get("compaction_inplace") + "\n\n" +
                                              pf.get("compaction_template", budget=1800, today=today)}]
            tools = self.tools_schema()  # the same tool list, so the cached prompt is reused as is
            self.ui.event("status", text="compacting context… (the conversation is already read; writing the "
                          "summary) · Esc cancels")
        else:
            budget_chars = int((ctx_len - 5000) * compaction.CHARS_PER_TOKEN * 0.85)
            convo = compaction.summary_request([m for m in middle if not m.get("_compaction")], self._summary,
                                               max_chars=budget_chars)
            tools = []
            self.ui.event("status", text="compacting context… the server reads the older turns first · Esc cancels")
        try:
            self._stream_task = asyncio.ensure_future(self.llm.stream(
                convo, tools, max_tokens=3000, on_content=on_content, cancel=self._cancel))
            turn = await self._stream_task
        except asyncio.CancelledError:
            self.ui.event("status", text="Compaction cancelled: nothing changed.")
            return None
        except LLMError as exc:
            self.ui.event("error", text=f"Compaction failed (nothing changed): {exc}")
            return None
        finally:
            self.cfg["model"]["thinking"] = saved
        if turn.interrupted or self._cancel.is_set():
            self.ui.event("status", text="Compaction cancelled: nothing changed.")
            return None
        summary = (turn.content or "").strip()
        if in_place and (turn.tool_calls or len(summary) < 200):
            return await self._write_summary_apart(middle, ctx_len, saved)  # it did not write a summary
        return summary or None

    async def _write_summary_apart(self, middle: list[dict], ctx_len: int, thinking) -> Optional[str]:
        self.cfg["model"]["thinking"] = False
        try:
            req = compaction.summary_request([m for m in middle if not m.get("_compaction")], self._summary,
                                             max_chars=int((ctx_len - 5000) * compaction.CHARS_PER_TOKEN * 0.85))
            self._stream_task = asyncio.ensure_future(self.llm.stream(req, [], max_tokens=3000, cancel=self._cancel))
            turn = await self._stream_task
        except (asyncio.CancelledError, LLMError):
            return None
        finally:
            self.cfg["model"]["thinking"] = thinking
        return None if turn.interrupted else ((turn.content or "").strip() or None)

    async def compact(self, reason: str = "auto") -> bool:
        ctx_len = int(self.cfg.get_path("model.context_length", 64000))
        limit, keep = self._limits(ctx_len)
        original = self.messages
        if self.cfg.get_path("agent.prune_old_outputs", True):
            # trim a copy: if that is not enough, the summary is written from the conversation as
            # llama-server has it cached (trimming changes it and would make it re-read everything)
            before = self._context_tokens()
            self.messages = copy.deepcopy(original)
            items, freed = self._prune(keep, limit)
            after = self._estimate()
            if items and after < limit * 0.8:  # enough room again: no summary needed
                self._measured = None
                self.ctx.state.setdefault("read_cache", {}).clear()
                self.last_prompt_tokens = after
                self.ui.event("pruned", items=items, before=before, after=after)
                self.save()
                return True
            trimmed, self.messages = self.messages, original
        else:
            trimmed = original
        start = compaction.split_point(trimmed, keep)
        if len(trimmed[1:start]) < 2 and start <= 1 and keep > 2000:
            start = compaction.split_point(trimmed, keep // 3)
        if int(self.cfg.get_path("agent.keep_recent_reads", 3) or 0):
            start = self._tail_with_reads(start, limit)
        middle = self.messages[1:start]
        if len(middle) < 2:
            return False
        summary = await self._write_summary(middle, ctx_len)
        if not summary:
            return False
        self._summary = summary
        # The request being worked on right now, if it is in the compacted part: the handoff would
        # otherwise tell the model that everything above was "already addressed" and it would stop.
        active = max((i for i, m in enumerate(self.messages) if m.get("role") == "user" and not m.get("_synthetic")),
                     default=0)
        if 0 < active < start:
            m = self.messages[active]
            self._active_request = str(m.get("_typed") or m.get("content") or "").strip()
        elif active:
            self._active_request = ""  # it is still in the recent part, word for word
        # active == 0: compacted at an earlier compaction, still being worked on — keep carrying it
        current = "\n\n" + pf.get("compaction_current", request=self._active_request) if self._active_request else ""
        facts = ""
        if self.cfg.get_path("agent.work_ledger", True):
            self.worklog.add_requests([m for i, m in enumerate(middle, 1) if i != active])
            last_said = next((str(m.get("content") or "") for m in reversed(middle)
                              if m.get("role") == "assistant" and str(m.get("content") or "").strip()), "")
            facts = self.worklog.render(self.ctx.cwd, last_said)
        todos = self.ctx.state.get("todos") or []
        todo_text = ("\n\n" + facts if facts else "") + ("\n\n## Current todo list\n" + "\n".join(f"- [{t['status']}] {t['id']}: {t['content']}" for t in todos)
                     if todos else "")
        if self.plan_text:
            todo_text += ("\n\n## The plan being carried out (from .hermes/plan.md)\n" + self.plan_text)
        if self.mode == "plan":  # the plan-mode instructions were in a message that is now summarized
            todo_text += "\n\n" + pf.get("plan_mode") + (self.plan_task or "")
        handoff = {"role": "user", "content": f"{pf.get("compaction_handoff")}\n\n{summary}{todo_text}{current}",
                   "_synthetic": True, "_compaction": True}
        tail = trimmed[start:]
        self.ctx.state.setdefault("read_cache", {}).clear()
        self.messages = [{"role": "system", "content": build_system_prompt(self.cfg, self.ctx, self.memory)}, handoff, *tail]
        self._measured = None
        self.last_prompt_tokens = self._estimate()
        self.ui.event("compacted", reason=reason, summary=summary, removed=len(middle))
        self.save()
        return True

    # ── tools ───────────────────────────────────────────────────────────
    def _run_tool_sync(self, call: ToolCall) -> str:
        tool = REGISTRY.get(call.name)
        if tool is None:
            return json.dumps({"success": False, "error": f"Unknown tool {call.name!r}. Available tools: "
                                                          f"{', '.join(REGISTRY)}."})
        if call.name in disabled_tools(self.cfg):
            return json.dumps({"success": False, "error": f"{call.name} is turned off by the user. Use the tools "
                               "you were given."})
        if tool.requires == "vision" and not self.llm.vision:
            return json.dumps({"success": False, "error": f"{call.name} is not available: llama-server has no vision "
                               "encoder (mmproj) loaded, so you cannot see images. Check the UI another way "
                               "(code, DOM, logs, curl, tests) or ask the user to look."})
        planning = self.mode == "plan" and not call.id.startswith("bang_")  # your own !commands are yours
        if planning and call.name in planmode.WRITE_TOOLS:
            return json.dumps({"success": False, "error": f"Plan mode: {call.name} is disabled — nothing may be "
                               "changed while planning. Put this change in the plan's Steps instead; the user "
                               "types /build to carry the plan out."})
        if planning and call.name == "terminal" and call.parsed is not None:
            command = str(call.parsed.get("command") or "")
            if not planmode.read_only_command(command) and self.cfg.get_path("agent.approvals", "smart") != "off":
                verdict = self.ctx.approve(command, "plan mode: this command may change something")
                if verdict not in ("once", "session"):
                    return json.dumps({"success": False, "error": "Plan mode: the user did not allow this "
                                       "command (only read-only commands run while planning). Plan the "
                                       "change instead."})
        if call.parsed is not None and self.cfg.get_path("agent.block_trash", True):
            args = call.parsed
            targets = [str(args[k]) for k in ("path", "workdir", "file_path") if isinstance(args.get(k), str)]
            command = str(args.get("command") or "") if call.name == "terminal" else ""
            if any(in_trash(self.ctx.resolve(t)) for t in targets) or \
                    (command and re.search(r"(?:^|[\s'\"=])(?:~|\$HOME|/home/[^/\s]+)?/?\.local/share/Trash\b|/\.Trash(?:-\d+)?/", command)):
                return json.dumps({"success": False, "error": "That is in the Trash: the user deleted it. Never read, "
                                   "search, run or change anything there. The project is " + self.ctx.cwd + " — work "
                                   "there; if what you need is missing, ask the user. (They can restore files from the "
                                   "Trash themselves.)"})
        if call.parsed is None and call.parse_error.startswith("This tool call was cut off"):
            return json.dumps({"success": False, "error": call.parse_error})
        if call.parsed is None:
            return json.dumps({"success": False, "error": f"Your arguments for {call.name} were not valid JSON "
                               f"({call.parse_error}). Re-emit the tool call with a valid JSON object."})

        def progress(text: str, _id=call.id):
            if self.loop:
                self.loop.call_soon_threadsafe(lambda: self.ui.event("tool_progress", id=_id, text=text))

        ctx = self.ctx
        ctx.progress = progress
        try:
            args = {**call.parsed, "_call_id": call.id} if call.name == "read_file" else call.parsed
            return tool.handler(args, ctx)
        except Exception as exc:  # a tool bug must never kill the loop
            return json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"})

    async def _run_tools(self, calls: list[ToolCall]) -> list[str]:
        self.ctx.state["live_call_ids"] = {m.get("tool_call_id") for m in self.messages
                                           if m.get("role") == "tool" and not m.get("_pruned")}
        async def one(call: ToolCall) -> str:
            tool = REGISTRY.get(call.name)
            try:  # a label is cosmetic: odd arguments must never take the app down
                label = tool.label(call.parsed or {}) if tool and call.parsed is not None else call.arguments[:80]
            except Exception:
                label = call.arguments[:80]
            self.ui.event("tool_start", id=call.id, name=call.name, label=label, emoji=tool.emoji if tool else "?",
                          args=call.parsed or {})
            t0 = time.monotonic()
            result = await asyncio.to_thread(self._run_tool_sync, call)
            try:
                d = json.loads(result)
                ok = d.get("success") is not False and not d.get("error") and d.get("exit_code", 0) in (0, None)
            except (ValueError, AttributeError):
                ok = True
            self.ui.event("tool_end", id=call.id, name=call.name, result=result, ok=ok,
                          duration=time.monotonic() - t0)
            if call.name == "todo_list":
                self.ui.event("todos", todos=list(self.ctx.state.get("todos", [])))
            try:
                self.worklog.record(call.name, call.parsed, result, self.ctx.cwd)
            except Exception:  # bookkeeping must never break a tool call
                pass
            return result

        if all(REGISTRY.get(c.name) and REGISTRY[c.name].read_only for c in calls) and len(calls) > 1:
            return list(await asyncio.gather(*(one(c) for c in calls)))
        results = []
        for c in calls:
            if self._cancel.is_set():
                results.append(json.dumps({"success": False, "error": "Not run: interrupted by the user."}))
                continue
            results.append(await one(c))
        return results

    # ── main loop ───────────────────────────────────────────────────────
    async def run(self, text: str, synthetic: bool = False) -> None:
        self.loop = asyncio.get_running_loop()
        self.busy = True
        self._cancel.clear()
        self.ctx.state["cancel"].clear()
        if not self.title and not synthetic:
            self.title = " ".join(text.split())[:60]
        if self.mode == "plan" and not synthetic:
            first = not self.plan_task
            self.plan_task = self.plan_task or text
            typed, text = text, pf.get("plan_mode" if first else "plan_followup") + text
        msg: dict = {"role": "user", "content": text}
        if self.mode == "plan" and not synthetic:
            msg["_typed"] = typed  # what the user wrote, for the compaction facts
        if synthetic:
            msg["_synthetic"] = True
        self.messages.append(msg)
        max_iter = int(self.cfg.get_path("agent.max_iterations", 150))
        ctx_len = int(self.cfg.get_path("model.context_length", 64000))
        guard = loopguard.settings(self.cfg)
        nudges, repeats, last_sig = 0, 0, None
        self.reads_in_row = 0
        loop_retries, think_budget = 0, None
        plan_rounds = 0
        no_tools_once, self.force_plan = self.force_plan, False
        try:
            had_vision = self.llm.vision
            if await self.llm.probe_vision() != had_vision and hasattr(self, "_schema_tokens"):
                del self._schema_tokens  # the tool list changed
            for iteration in range(max_iter + 1):
                limit, _ = self._limits(ctx_len)
                if self._context_tokens() > limit:
                    await self.compact()
                final_round = iteration == max_iter
                if final_round:
                    self.messages.append({"role": "user", "content": pf.get("iteration_budget"), "_synthetic": True})
                plan_limit = int(self.cfg.get_path("agent.plan_max_steps", 20))
                if self.mode == "plan" and plan_rounds >= plan_limit and not no_tools_once:
                    no_tools_once = True  # investigated long enough: the next reply must be the plan
                    self.messages.append({"role": "user", "content": pf.get("plan_write_now"), "_synthetic": True})
                no_tools = final_round or no_tools_once
                no_tools_once = False
                turn = await self._stream_turn(tools=[] if no_tools else self.tools_schema(), ctx_len=ctx_len,
                                               think_budget=think_budget)
                if turn is None:  # compacted after an overflow; retry
                    continue
                think_budget = None
                if turn.loop_ended:
                    self.ui.event("loop_stopped", excerpt=turn.loop_ended, how="ended")
                if turn.loop_abort and not turn.interrupted:
                    loop_retries += 1
                    if loop_retries > int(guard.get("max_retries", 2)):
                        self.ui.event("loop_stopped", excerpt=turn.loop_abort, how="gave_up")
                        self.messages.append({"role": "assistant", "content": "(Stopped: I kept going in circles on "
                                              "this. Tell me which approach to take, or give me more detail.)"})
                        return
                    self.ui.event("loop_stopped", excerpt=turn.loop_abort, how="retry")
                    self.messages.append({"role": "user", "_synthetic": True, "content": pf.get("loop_retry", 
                        excerpt=turn.loop_abort[:160])})
                    think_budget = int(guard.get("retry_budget", 2048))
                    continue
                if turn.interrupted:
                    if turn.content.strip():
                        self.messages.append({"role": "assistant", "content": turn.content + "\n\n[interrupted]"})
                    self.ui.event("interrupted")
                    return
                self._retire_images(turn)
                assistant: dict = {"role": "assistant", "content": turn.content}
                if turn.reasoning:
                    assistant["reasoning_content"] = turn.reasoning
                if not turn.tool_calls:
                    if not turn.content.strip() and nudges < 2:
                        nudges += 1
                        if turn.reasoning:
                            self.messages.append(assistant)
                        self.messages.append({"role": "user", "content": pf.get("thought_only" if turn.reasoning else "empty_reply"),
                                              "_synthetic": True})
                        self.ui.event("status", text="nudging the model to act…")
                        continue
                    self.messages.append(assistant)
                    self.ui.event("final", text=turn.content)
                    if self.mode == "plan" and planmode.looks_like_plan(turn.content):
                        path = planmode.save_plan(self.ctx.cwd, self.plan_task, turn.content)
                        self.plan_written = planmode.load_plan(self.ctx.cwd) or ""
                        self.ui.event("plan_saved", path=str(path), steps=len(planmode.steps(turn.content)))
                    await self._maybe_review(ctx_len)
                    return
                assistant["tool_calls"] = [c.as_message() for c in turn.tool_calls]
                self.messages.append(assistant)
                results = await self._run_tools(turn.tool_calls)
                self.iters_since_review += 1
                self.iters_since_notes += 1
                if any(c.name == "skill_manage" and '"success": true' in r for c, r in zip(turn.tool_calls, results)):
                    self.iters_since_review = 0  # the model already saved a skill on its own
                sig = json.dumps([(c.name, c.arguments) for c in turn.tool_calls], sort_keys=True)
                repeats = repeats + 1 if sig == last_sig else 1
                last_sig = sig
                if all(c.name in ("read_file", "search_files") for c in turn.tool_calls):
                    self.reads_in_row += len(turn.tool_calls)
                else:
                    self.reads_in_row = 0
                read_note = self.reads_in_row >= 12 and (self.reads_in_row - 12) % 8 < len(turn.tool_calls)
                if self.mode == "plan":
                    plan_rounds += 1
                    if plan_rounds in (10, 15):
                        read_note = False
                        results = results[:-1] + [results[-1] + pf.get("plan_nudge", n=plan_rounds)]
                for i, (call, result) in enumerate(zip(turn.tool_calls, results)):
                    if read_note and i == len(results) - 1:
                        result += pf.get("reading_streak", n=self.reads_in_row)
                    if repeats >= 3 and i == len(results) - 1:
                        result += pf.get("repeated_call", n=repeats)
                    self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result})
                self._attach_pending_images()
                for note in self.notifications:
                    self.messages.append({"role": "user", "content": note, "_synthetic": True})
                self.notifications.clear()
                for s in self.steer_queue:
                    self.messages.append({"role": "user", "content": f"{STEER_OPEN}\n{s}\n{STEER_CLOSE}", "_synthetic": True})
                    self.ui.event("steered", text=s)
                self.steer_queue.clear()
                if self._cancel.is_set():
                    self.ui.event("interrupted")
                    return
                nudges = 0
        except LLMError as exc:
            self.ui.event("error", text=str(exc))
        except Exception as exc:  # a bug must cost one turn, never the app and the session
            import traceback
            log = self.home / "logs" / "crash.log"
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{traceback.format_exc()}")
            except OSError:
                pass
            self.ui.event("error", text=f"Hermes Lite hit an internal error and stopped this turn: "
                                        f"{type(exc).__name__}: {exc}. Details: {log}. Your session is intact; "
                                        "send your message again or rephrase it.")
        finally:
            self.busy = False
            self._cancel.clear()
            self.ctx.state["cancel"].clear()
            self.save()
            self.ui.event("idle")

    # ── skill review (Hermes' background review, run in the foreground on one local server) ──
    async def _maybe_review(self, ctx_len: int) -> None:
        if self.cfg.get_path("skills.auto_review", True) and \
                self.iters_since_review >= int(self.cfg.get_path("skills.review_after_tool_calls", 10)):
            await self.review_skills(ctx_len)
        every = int(self.cfg.get_path("agent.project_notes_after_tool_calls", 8))
        path = projectnotes.notes_path(self.cfg, self.ctx.cwd)
        first = path is not None and not path.exists()  # no notes yet: write the first version early
        if path is not None and not self._cancel.is_set() and self.iters_since_notes >= (min(3, every) if first else every):
            await self.update_project_notes("auto")

    # ── project notes (.hermes/project.md) ─────────────────────────────
    def notes_pending(self) -> bool:
        """Work since the last update worth writing down (for /new and quitting)."""
        return self.iters_since_notes >= 3 and projectnotes.notes_path(self.cfg, self.ctx.cwd) is not None

    async def notes_now(self, reason: str = "manual") -> str:
        """/notes update, /new, quitting: update the notes outside a turn (Esc skips)."""
        self.loop = asyncio.get_running_loop()
        self.busy = True
        self._cancel.clear()
        try:
            return await self.update_project_notes(reason)
        finally:
            self.busy = False
            self._cancel.clear()
            self.ctx.state["cancel"].clear()

    async def update_project_notes(self, reason: str = "auto") -> str:
        """Ask the model to rewrite the project notes, at the end of the conversation llama-server
        already has cached; write the file. Returns saved | skipped | nothing | failed."""
        path = projectnotes.notes_path(self.cfg, self.ctx.cwd)
        if path is None:
            return "nothing"
        if reason != "manual" and self.iters_since_notes == 0:
            return "nothing"
        ctx_len = int(self.cfg.get_path("model.context_length", 64000))
        limit = int(self.cfg.get_path("agent.project_notes_words", 900))
        today = dt.date.today().strftime("%Y-%m-%d")
        existing = projectnotes.load(path)
        # Writing is the slow part (a few hundred words a minute here): only the first version, and
        # a tidy-up once the 'Done so far' list grows long, rewrite the whole file. Every other
        # update sends just what changed: new bullets, changed sections.
        full = reason == "rewrite" or not existing or projectnotes.done_bullets(existing) > 30
        if full:
            facts = self.worklog.render(self.ctx.cwd) or "(none)"
            request = pf.get("project_notes_update", current=existing or "(none yet: this is the first version)",
                             facts=facts, limit=limit, today=today)
            max_tokens = int(limit * 2.2) + 400
        else:
            request = pf.get("project_notes_delta", current=existing, today=today)
            max_tokens = int(self.cfg.get_path("agent.project_notes_delta_tokens", 700))
        need = int(len(request) / compaction.CHARS_PER_TOKEN) + max_tokens + 600
        if self._context_tokens() + need > ctx_len:
            self.ui.event("notes_end", outcome="failed", path=str(path),
                          detail="the context is too full to write them now; /compress, then /notes update")
            return "failed"
        convo = self._wire_messages() + [{"role": "user", "content": request}]
        self.reviewing = True
        saved_thinking = self.cfg["model"].get("thinking", True)
        self.cfg["model"]["thinking"] = False
        written = [0]

        def progress(text: str) -> None:
            written[0] += len(text)
            if written[0] // 400 != (written[0] - len(text)) // 400:
                self.ui.event("status", text=f"updating the project notes… ~{int(written[0] / 4.5)} words · Esc skips")

        self.ui.event("notes_start", path=str(path))
        outcome, detail = "failed", ""
        try:
            # the same tool list as the conversation, so llama-server's cached prompt is reused as is
            self._stream_task = asyncio.ensure_future(self.llm.stream(
                convo, self.tools_schema(), max_tokens=max_tokens, on_content=progress, cancel=self._cancel))
            turn = await self._stream_task
            if turn.interrupted or self._cancel.is_set():
                outcome = "skipped"
            elif full and (text := projectnotes.clean(turn.content)):
                projectnotes.save(path, text)
                self.iters_since_notes = 0
                outcome, detail = "saved", f"{len(text.split())} words"
            elif not full:
                self.iters_since_notes = 0
                merged = projectnotes.merge(existing, turn.content)
                if merged:
                    projectnotes.save(path, merged)
                    outcome, detail = "saved", f"{len((turn.content or '').split())} words of changes"
                else:
                    outcome, detail = "unchanged", ""
            else:
                detail = "the reply was not a notes file; nothing changed"
        except asyncio.CancelledError:
            outcome = "skipped"
        except LLMError as exc:
            detail = str(exc)
        finally:
            self.cfg["model"]["thinking"] = saved_thinking
            self.reviewing = False
            self.ui.event("notes_end", outcome=outcome, path=str(path), detail=detail)
        return outcome

    async def learn(self) -> None:
        """/learn: run the skill review now, whatever the counter says."""
        self.loop = asyncio.get_running_loop()
        self.busy = True
        self._cancel.clear()
        try:
            await self.review_skills(int(self.cfg.get_path("model.context_length", 64000)))
        except LLMError as exc:
            self.ui.event("error", text=str(exc))
        finally:
            self.busy = False
            self._cancel.clear()
            self.ctx.state["cancel"].clear()
            self.ui.event("idle")

    async def review_skills(self, ctx_len: int) -> None:
        """Append a review request to the conversation (its prefix is already in llama-server's cache,
        so this costs only the reply), let the model use the skill tools only, then drop the whole
        exchange so the main history stays as it was."""
        from .tools.skills import catalog
        off = disabled_tools(self.cfg)
        if not all(n in REGISTRY and n not in off for n in ("skill_view", "skill_manage")):
            return
        review_tools = [n for n in REVIEW_TOOLS if n in REGISTRY and n not in off]  # the skills are listed in the request
        self.iters_since_review = 0
        self.reviewing = True
        listing = "\n".join(f"- {n}: {i['description']}" for n, i in sorted(catalog(self.ctx).items())) or "(none yet)"
        convo = self._wire_messages() + [{"role": "user", "content": pf.get("skill_review", skills=listing)}]
        tools = self.tools_schema()  # the conversation's own tool list: llama-server's cached prompt stays valid
        saved: list[str] = []
        outcome = "nothing"
        saved_thinking = self.cfg["model"].get("thinking", True)
        self.cfg["model"]["thinking"] = bool(self.cfg.get_path("skills.review_thinking", False))
        self.ui.event("review_start")
        try:
            for _ in range(int(self.cfg.get_path("skills.review_max_steps", 8))):
                if self._cancel.is_set():
                    outcome = "skipped"
                    break
                turn = await self._stream_turn(tools, ctx_len, messages=convo)
                if turn is None or turn.interrupted:
                    outcome = "skipped"
                    break
                msg: dict = {"role": "assistant", "content": turn.content}
                if not turn.tool_calls:
                    outcome = "saved" if saved else "nothing"
                    break
                msg["tool_calls"] = [c.as_message() for c in turn.tool_calls]
                convo.append(msg)
                allowed = [c for c in turn.tool_calls if c.name in review_tools]
                results = await self._run_tools(allowed) if allowed else []
                by_id = {c.id: r for c, r in zip(allowed, results)}
                for c in turn.tool_calls:
                    r = by_id.get(c.id, json.dumps({"success": False, "error": "Only " + ", ".join(review_tools)
                                                                                 + " are allowed here."}))
                    if c.name == "skill_manage" and '"success": true' in r:
                        try:
                            saved += json.loads(r).get("results", [])
                        except ValueError:
                            saved.append("skill updated")
                    convo.append({"role": "tool", "tool_call_id": c.id, "name": c.name, "content": r})
            else:
                outcome = "saved" if saved else "nothing"
        finally:
            self.cfg["model"]["thinking"] = saved_thinking
            self.reviewing = False
            self.ui.event("review_end", outcome=outcome, saved=saved)

    async def _stream_turn(self, tools: list[dict], ctx_len: int, messages: Optional[list] = None,
                           think_budget: Optional[int] = None) -> Optional[Turn]:
        self.ui.event("assistant_start")
        t0 = time.monotonic()
        sent = len(self.messages) if messages is None else 0
        self._stream_task = asyncio.ensure_future(self.llm.stream(
            messages if messages is not None else self._wire_messages(), tools,
            on_reasoning=lambda s: self.ui.event("reasoning", text=s),
            on_content=lambda s: self.ui.event("content", text=s),
            on_tool_draft=lambda i, n, a: self.ui.event("tool_draft", index=i, name=n, args=a),
            cancel=self._cancel, think_budget=think_budget))
        try:
            turn = await self._stream_task
        except LLMError as exc:
            if exc.context_overflow and await self.compact("overflow"):
                return None
            raise
        except asyncio.CancelledError:
            turn = Turn(interrupted=True)
        if turn.prompt_tokens:
            if sent and tools and len(self.messages) >= sent:
                self._measured = (turn.prompt_tokens, sent, self.messages[sent - 1])
            # what stays in the context: the prompt plus the visible reply (its thinking is not sent back)
            visible = len(turn.content or "") + sum(len(c.arguments or "") + 60 for c in turn.tool_calls)
            self.last_prompt_tokens = turn.prompt_tokens + int(visible / compaction.CHARS_PER_TOKEN)
        if turn.tok_per_s:
            self.last_tok_s = turn.tok_per_s
        self.ui.event("assistant_end", tokens=turn.completion_tokens, tok_s=turn.tok_per_s,
                      context=self.last_prompt_tokens, ctx_len=ctx_len, seconds=time.monotonic() - t0,
                      finish=turn.finish_reason)
        return turn


def _prune_scratch(root: Path, days: float = 7) -> None:
    """Session scratch folders (outputs, temp files) older than a week are removed."""
    import shutil
    cutoff = time.time() - days * 86400
    for d in root.glob("*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def list_sessions(limit: int = 30) -> list[dict]:
    out = []
    for p in sorted((home() / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "title": d.get("title") or "(untitled)", "updated": d.get("updated", 0),
                        "turns": sum(1 for m in d["messages"] if m["role"] == "user" and not m.get("_synthetic")),
                        "path": str(p)})
        except (OSError, ValueError, KeyError):
            continue
    return out


def load_session(session_id: str) -> Optional[dict]:
    p = home() / "sessions" / f"{session_id}.json"
    if not p.exists():
        matches = list((home() / "sessions").glob(f"{session_id}*.json"))
        if len(matches) != 1:
            return None
        p = matches[0]
    return json.loads(p.read_text(encoding="utf-8"))
