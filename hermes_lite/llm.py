"""Streaming client for llama-server's OpenAI-compatible /v1/chat/completions.

Handles: reasoning_content deltas (or inline <think>), streamed tool_call deltas, usage/timings, and a
fallback parser for tool calls a small model leaks into plain content (<tool_call> JSON or the
Qwen3-Coder <function=...><parameter=...> XML form).
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from .config import Config

MARKERS = ("<tool_call>", "<function=")


class LLMError(Exception):
    def __init__(self, message: str, status: int = 0, context_overflow: bool = False, server_died: bool = False):
        super().__init__(message)
        self.status = status
        self.context_overflow = context_overflow
        self.server_died = server_died


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as produced by the model
    parsed: Optional[dict] = None
    parse_error: str = ""

    def as_message(self) -> dict:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class Turn:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tok_per_s: float = 0.0
    interrupted: bool = False
    loop_ended: str = ""   # thinking loop detected and ended by llama-server; the reply went on
    loop_abort: str = ""   # loop detected and the reply was stopped: resend with a note


def parse_arguments(raw: str) -> tuple[Optional[dict], str]:
    """Lenient JSON-object parse; returns (dict, "") or (None, error)."""
    text = (raw or "").strip()
    if not text:
        return {}, ""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    for attempt in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            val = json.loads(attempt)
            if isinstance(val, str):  # double-encoded
                val = json.loads(val)
            if isinstance(val, dict):
                return val, ""
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        val = ast.literal_eval(text)
        if isinstance(val, dict):
            return val, ""
    except (ValueError, SyntaxError):
        pass
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} at line {exc.lineno} column {exc.colno}"
    return None, "arguments must be a JSON object"


def _coerce(value: str, schema: dict) -> Any:
    kind = (schema or {}).get("type")
    if kind == "string" or (not kind and value.strip()[:1] not in "[{"):
        return value  # keep a file's own leading/trailing newlines
    v = value.strip("\n")
    if kind in ("integer", "number", "boolean", "array", "object") or (not kind and v[:1] in "[{"):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            if kind == "boolean":
                return v.strip().lower() in ("true", "yes", "1")
    return v


CUT_OFF = ("This tool call was cut off before it was finished (the reply hit max_tokens or the stream "
           "broke), so nothing was run. Send it again, complete; split a very large file into a first "
           "write_file plus patch calls that add the rest.")


def extract_inline_tool_calls(content: str, tool_schemas: dict[str, dict],
                              cut_off: bool = False) -> tuple[str, list[ToolCall]]:
    """Pull tool calls out of plain text. Returns (content_without_calls, calls). With cut_off (the
    reply ended early), an unfinished last call is returned with a parse error instead of being run."""
    calls: list[ToolCall] = []
    start = min((i for i in (content.find(m) for m in MARKERS) if i != -1), default=-1)
    if start == -1:
        return content, calls
    tail = content[start:]
    for m in re.finditer(r"<function=([\w.-]+)>(.*?)(?:</function>|$)", tail, re.S):
        name, body = m.group(1), m.group(2)
        props = (tool_schemas.get(name) or {}).get("parameters", {}).get("properties", {})
        args = {p.group(1): _coerce(p.group(2), props.get(p.group(1), {}))
                for p in re.finditer(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", body, re.S)}
        finished = m.group(0).endswith("</function>")
        calls.append(ToolCall(id="call_" + uuid.uuid4().hex[:10], name=name,
                              arguments=json.dumps(args, ensure_ascii=False),
                              parsed=None if (cut_off and not finished) else args,
                              parse_error=CUT_OFF if (cut_off and not finished) else ""))
    if not calls:
        for m in re.finditer(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", tail, re.S):
            obj, err = parse_arguments(m.group(1))
            if obj and obj.get("name"):
                args = obj.get("arguments", obj.get("parameters", {}))
                if isinstance(args, str):
                    args, _ = parse_arguments(args)
                calls.append(ToolCall(id="call_" + uuid.uuid4().hex[:10], name=str(obj["name"]),
                                      arguments=json.dumps(args or {}, ensure_ascii=False), parsed=args or {}))
    return (content[:start].rstrip() if calls else content), calls


def _trailing_call(reasoning: str) -> int | None:
    """Index where the run of complete tool calls that *ends* the thinking starts (None when the
    thinking merely mentions the syntax, or the call is unfinished)."""
    block = re.compile(r"\s*(<tool_call>\s*)?<function=[\w.-]+>(?:(?!<function=|<tool_call>).)*?</function>"
                       r"\s*(</tool_call>)?\s*", re.S)
    for m in re.finditer(r"<tool_call>|<function=", reasoning):
        pos, i = m.start(), m.start()
        while True:
            b = block.match(reasoning, i)
            if not b or b.end() == i:
                break
            i = b.end()
        if i > pos and not reasoning[i:].strip():
            return pos
    return None


class _ThinkSplitter:
    """Routes inline <think>...</think> text to reasoning when the server didn't split it."""

    def __init__(self):
        self.buf, self.in_think, self.started = "", False, False

    def feed(self, text: str) -> tuple[str, str]:
        self.buf += text
        reasoning, content = "", ""
        while self.buf:
            if not self.started:
                stripped = self.buf.lstrip()
                if len(stripped) < 7 and "<think>".startswith(stripped):
                    return reasoning, content
                self.started = True
                if stripped.startswith("<think>"):
                    self.in_think, self.buf = True, stripped[7:]
                    continue
            if self.in_think:
                i = self.buf.find("</think>")
                if i == -1:
                    keep = max(0, len(self.buf) - 8)
                    reasoning += self.buf[:keep]
                    self.buf = self.buf[keep:]
                    return reasoning, content
                reasoning += self.buf[:i]
                self.buf, self.in_think = self.buf[i + 8:].lstrip("\n"), False
                continue
            content += self.buf
            self.buf = ""
        return reasoning, content

    def flush(self) -> tuple[str, str]:
        rest, self.buf = self.buf, ""
        return (rest, "") if self.in_think else ("", rest)


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.get_path("model.base_url", "http://127.0.0.1:8091/v1").rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10))
        from .config import home
        self.stream_log = home() / "logs" / "stream.log"
        self.rx = {"thinking": 0, "text": 0, "tool": 0}  # chars received in the current reply
        self.rx_tail = ""                                # last part of what the server sent, raw
        self.client_tools: Optional[bool] = None  # decided once per server by probe_tool_parsing()
        self.vision: Optional[bool] = None        # decided once per server by probe_vision()
        self.last_data = 0.0                      # monotonic time of the last streamed chunk
        self.control_ok: Optional[bool] = None    # llama-server can end a thinking block on request

    async def probe_control(self) -> bool:
        """Does this llama-server have /v1/chat/completions/control (end thinking mid-stream)?"""
        if self.control_ok is not None:
            return self.control_ok
        try:
            r = await self.client.post(f"{self.base}/chat/completions/control",
                                       json={"id": "hermes-lite-probe", "action": "reasoning_end"}, timeout=10)
        except httpx.ConnectError:
            return False
        except httpx.HTTPError:
            self.control_ok = False
            return False
        self.control_ok = r.status_code != 404 and r.status_code != 405
        return self.control_ok

    async def _end_thinking(self, completion_id: str) -> bool:
        try:
            r = await self.client.post(f"{self.base}/chat/completions/control",
                                       json={"id": completion_id, "action": "reasoning_end"}, timeout=10)
            return r.status_code == 200 and bool((r.json() or {}).get("success", True))
        except (httpx.HTTPError, ValueError):
            return False

    async def probe_vision(self) -> bool:
        """Does llama-server take images (an mmproj is loaded)? Read from /props once per server."""
        if self.vision is not None:
            return self.vision
        root = self.base[:-3] if self.base.endswith("/v1") else self.base
        try:
            r = await self.client.get(f"{root}/props", timeout=10)
            props = r.json() if r.status_code == 200 else {}
        except httpx.ConnectError:
            return False  # not up yet: ask again next time
        except (httpx.HTTPError, ValueError):
            props = {}
        mods = props.get("modalities") or {}
        self.vision = bool(mods.get("vision")) or "image" in (props.get("input_modalities") or [])
        return self.vision

    async def probe_tool_parsing(self, tools: list[dict]) -> bool:
        """Can Hermes Lite parse tool calls itself? Only if llama-server still renders the tool list
        into the prompt with tool_choice="none" (checked with its /apply-template endpoint)."""
        mode = str(self.cfg.get_path("model.tool_parsing", "auto")).lower()
        if mode in ("server", "client"):
            self.client_tools = mode == "client"
            return self.client_tools
        if self.client_tools is not None:
            return self.client_tools
        root = self.base[:-3] if self.base.endswith("/v1") else self.base
        name = tools[0]["function"]["name"]
        ok = False
        try:
            r = await self.client.post(f"{root}/apply-template", json={
                "messages": [{"role": "user", "content": "hi"}], "tools": tools[:2], "tool_choice": "none"})
            prompt = r.json().get("prompt", "") if r.status_code == 200 else ""
            ok = f'"{name}"' in prompt and ("<tool_call>" in prompt or "<function=" in prompt)
        except httpx.ConnectError:
            return False  # server not up yet: decide on the next request
        except (httpx.HTTPError, ValueError):
            ok = False
        self.client_tools = ok
        return ok

    async def aclose(self):
        await self.client.aclose()

    def _body(self, messages: list[dict], tools: list[dict], max_tokens: Optional[int],
              think_budget: Optional[int] = None) -> dict:
        from .loopguard import settings as guard_settings
        m = self.cfg["model"]
        guard = guard_settings(self.cfg)
        body = {
            "model": m.get("name", "local"), "messages": messages, "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": m.get("temperature", 0.6), "top_p": m.get("top_p", 0.95),
            "top_k": m.get("top_k", 20), "min_p": m.get("min_p", 0.0),
            "presence_penalty": m.get("presence_penalty", 0.0),
            "max_tokens": max_tokens or m.get("max_tokens", 16384),
            "chat_template_kwargs": {"enable_thinking": bool(m.get("thinking", True))},
        }
        if guard:
            body["temperature"] = guard["temperature"]
            body["presence_penalty"] = guard["presence_penalty"]
            if float(guard.get("dry_multiplier") or 0) > 0:
                body["dry_multiplier"] = float(guard["dry_multiplier"])
            budget = think_budget if think_budget is not None else int(guard.get("thinking_budget") or 0)
            if budget > 0 and m.get("thinking", True):
                body["reasoning_budget_tokens"] = budget
                from . import promptfiles
                body["reasoning_budget_message"] = guard.get("budget_message") or promptfiles.get("thinking_budget")
            if self.control_ok:
                body["reasoning_control"] = True
        if tools:
            client = bool(self.client_tools) if self.client_tools is not None else \
                str(m.get("tool_parsing", "auto")).lower() == "client"
            # tool_choice "none" keeps the tools in the prompt but stops llama-server from parsing
            # (and buffering) the calls: they stream as text and extract_inline_tool_calls reads them.
            body.update({"tools": tools, "tool_choice": "none" if client else "auto", "parallel_tool_calls": True})
            if client:
                body["stop"] = ["<tool_response>"]
        return body

    async def stream(self, messages: list[dict], tools: list[dict], *,
                     on_reasoning: Callable[[str], None] = lambda s: None,
                     on_content: Callable[[str], None] = lambda s: None,
                     on_tool_draft: Callable[[int, str, str], None] = lambda i, n, a: None,
                     cancel: Optional[asyncio.Event] = None,
                     max_tokens: Optional[int] = None,
                     think_budget: Optional[int] = None) -> Turn:
        if tools and self.client_tools is None:
            await self.probe_tool_parsing(tools)
        from .loopguard import LoopDetector, settings as guard_settings
        guard = guard_settings(self.cfg)
        if guard and self.control_ok is None:
            await self.probe_control()
        body = self._body(messages, tools, max_tokens, think_budget)
        watch = guard and guard.get("detect", True)
        think_watch = LoopDetector()  # thinking only
        completion_id = ""
        self.rx, self.rx_tail = {"thinking": 0, "text": 0, "tool": 0}, ""
        log = None
        try:
            self.stream_log.parent.mkdir(parents=True, exist_ok=True)
            log = open(self.stream_log, "w", encoding="utf-8", buffering=1)
            log.write(f"=== {time.strftime('%H:%M:%S')} new reply · tool_choice={body.get('tool_choice', '-')} "
                      f"· {len(messages)} messages ===\n")
        except OSError:
            log = None
        last_kind = ""

        def record(kind: str, text: str) -> None:
            nonlocal last_kind
            self.rx[kind] += len(text)
            self.rx_tail = (self.rx_tail + text)[-4000:]
            if log is not None:
                try:
                    if kind != last_kind:
                        log.write(f"\n--- {kind} ---\n")
                    log.write(text)
                    if log.tell() > 20_000_000:
                        log.seek(0)
                        log.truncate()
                except (OSError, ValueError):
                    pass
            last_kind = kind

        turn = Turn()
        partial: dict[int, dict] = {}
        splitter = _ThinkSplitter()
        shown = 0  # chars of content forwarded to the UI (we hide leaked tool-call markup)

        def emit_content(text: str):
            nonlocal shown
            if not text:
                return
            turn.content += text
            visible = turn.content
            cut = min((i for i in (visible.find(mk) for mk in MARKERS) if i != -1), default=-1)
            if cut != -1:
                visible = visible[:cut]
            else:  # hold back a possible partial marker at the end
                for mk in MARKERS:
                    for k in range(len(mk) - 1, 0, -1):
                        if visible.endswith(mk[:k]):
                            visible = visible[:-k]
                            break
            if len(visible) > shown:
                on_content(visible[shown:])
                shown = len(visible)
            if cut != -1:  # tool call written as text: let the UI show it being drafted
                on_tool_draft(-1, "", turn.content[cut:])

        try:
            async with self.client.stream("POST", f"{self.base}/chat/completions",
                                          json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode(errors="replace")
                    overflow = "context" in text.lower() and ("exceed" in text.lower() or "too long" in text.lower())
                    raise LLMError(f"llama-server HTTP {resp.status_code}: {text[:800]}", resp.status_code, overflow)
                async for line in resp.aiter_lines():
                    if cancel is not None and cancel.is_set():
                        turn.interrupted = True
                        break
                    if not line.startswith("data:"):
                        continue
                    self.last_data = time.monotonic()
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        msg = json.dumps(chunk["error"])[:800]
                        raise LLMError("llama-server error: " + msg, 0, "context" in msg.lower())
                    if usage := chunk.get("usage"):
                        turn.prompt_tokens = usage.get("prompt_tokens", turn.prompt_tokens)
                        turn.completion_tokens = usage.get("completion_tokens", turn.completion_tokens)
                    if timings := chunk.get("timings"):
                        turn.tok_per_s = float(timings.get("predicted_per_second") or turn.tok_per_s)
                    completion_id = chunk.get("id") or completion_id
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if r := delta.get("reasoning_content") or delta.get("reasoning"):
                            record("thinking", r)
                            turn.reasoning += r
                            if watch and not turn.loop_ended and (hit := think_watch.feed(r)):
                                # going in circles: end the thinking on the spot if the server can,
                                # otherwise stop here and let the agent resend with a note
                                if completion_id and self.control_ok and await self._end_thinking(completion_id):
                                    turn.loop_ended = hit
                                    record("thinking", "\n[Hermes Lite: thinking loop — thinking ended here]\n")
                                else:
                                    turn.loop_abort = hit
                                    break
                            on_reasoning(r)
                        if c := delta.get("content"):
                            record("text", c)
                            think, text = splitter.feed(c)
                            if think:
                                turn.reasoning += think
                                on_reasoning(think)
                            emit_content(text)
                        for tc in delta.get("tool_calls") or []:
                            slot = partial.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            record("tool", (f"[{fn['name']}] " if fn.get("name") else "") + (fn.get("arguments") or ""))
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                            on_tool_draft(tc.get("index", 0), slot["name"], slot["arguments"])
                        if choice.get("finish_reason"):
                            turn.finish_reason = choice["finish_reason"]
                    if turn.loop_abort:
                        turn.finish_reason = "loop"
                        break  # closing the stream makes llama-server stop generating
        except asyncio.CancelledError:  # user interrupt: keep what streamed so far
            turn.interrupted = True
        except httpx.ConnectError as exc:
            raise LLMError(f"Cannot reach llama-server at {self.base}: {exc}") from exc
        except httpx.HTTPError as exc:
            if not turn.content and not turn.reasoning:
                raise LLMError(f"llama-server closed the connection before replying ({exc}). "
                               "It most likely crashed while reading the prompt.", server_died=True) from exc
            turn.finish_reason = turn.finish_reason or "error"
        if log is not None:
            try:
                log.write(f"\n=== end · finish={turn.finish_reason or ('interrupted' if turn.interrupted else '?')} ===\n")
                log.close()
            except (OSError, ValueError):
                pass
        think, text = splitter.flush()
        if think:
            turn.reasoning += think
            on_reasoning(think)
        emit_content(text)
        for _, slot in sorted(partial.items()):
            if slot["name"]:
                parsed, err = parse_arguments(slot["arguments"])
                turn.tool_calls.append(ToolCall(id=slot["id"] or "call_" + uuid.uuid4().hex[:10], name=slot["name"].strip(),
                                                arguments=slot["arguments"] or "{}", parsed=parsed, parse_error=err))
        schemas = {t["function"]["name"]: t["function"] for t in tools or []}
        cut_off = turn.finish_reason in ("length", "error", "loop")
        if not turn.tool_calls and any(mk in turn.content for mk in MARKERS):
            turn.content, turn.tool_calls = extract_inline_tool_calls(turn.content, schemas, cut_off=cut_off)
        if not turn.tool_calls and tools and not turn.content.strip():
            # Qwen sometimes goes from thinking straight into <tool_call> without closing </think>; some
            # llama-server builds then leave the call inside reasoning_content, and it would never run.
            start = _trailing_call(turn.reasoning)
            if start is not None:
                rest, calls = extract_inline_tool_calls(turn.reasoning[start:], schemas, cut_off=cut_off)
                if calls and all(c.name in schemas for c in calls):
                    turn.reasoning = turn.reasoning[:start].rstrip()
                    turn.tool_calls = calls
        return turn
