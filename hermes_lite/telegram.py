"""Telegram bridge: talk to the running Hermes Lite from your phone (/telegram on), or run it with no
TUI (hermes-lite --telegram).

Hermes Lite asks Telegram for new messages itself (long polling), so nothing has to be reachable
from the internet. Only one Telegram account is ever answered: the one that sent the pairing code
shown at the computer. Everyone else is ignored without a reply. A sudo password is never asked
for through the chat."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

API = "https://api.telegram.org"
LIMIT = 4000                 # Telegram allows 4096 characters per message
PROGRESS_LINES = 8
EDIT_EVERY = 2.5             # seconds between edits of the progress message (Telegram rate limits)
TRANSPORT: Optional["httpx.AsyncBaseTransport"] = None   # tests: a fake Telegram


# ── settings: token, paired account ───────────────────────────────────────
def state_path(home: Path) -> Path:
    return home / "telegram.json"


def load_state(home: Path) -> dict:
    try:
        data = json.loads(state_path(home).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(home: Path, data: dict) -> None:
    """Only you can read it: it holds the bot token."""
    path = state_path(home)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def token(home: Path) -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or str(load_state(home).get("token") or "")


TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


# ── formatting ────────────────────────────────────────────────────────────
def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split on line breaks where possible; nothing is lost."""
    text = text.strip() or "(empty)"
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text]


def to_telegram_markdown(text: str) -> str:
    """The model writes CommonMark; Telegram's legacy Markdown knows *bold*, _italic_, `code`, ```pre```.
    Headings and **bold** become *bold*; code is left as it is. If Telegram still refuses it, the
    message is sent as plain text."""
    out, in_code = [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code:
            line = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", line)
            line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
            line = re.sub(r"(?<![\w*])__(.+?)__(?![\w*])", r"_\1_", line)
        out.append(line)
    return "\n".join(out)


def format_questions(questions: list[dict]) -> str:
    """The clarify tool's questions as one message: a letter picks a choice."""
    rows = ["❓ Hermes needs a decision. Reply with one line per question (a letter picks a choice; for "
            "several choices: a,c). Or answer at the computer."]
    for i, q in enumerate(questions, 1):
        rows.append(f"\n{i}. {q['question']}" + ("  (several allowed)" if q.get("multi_select") else ""))
        rows += [f"   {chr(97 + j)}) {c}" for j, c in enumerate(q.get("choices") or [])]
    return "\n".join(rows)


def parse_answers(text: str, questions: list[dict]) -> list:
    """Your reply → one answer per question, as the clarify dialog gives them."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if len(questions) == 1:
        lines = [" ".join(lines)] if lines else []
    out: list = []
    for i, q in enumerate(questions):
        line = re.sub(r"^\s*\d+[.)]\s*", "", lines[i]) if i < len(lines) else ""
        choices = q.get("choices") or []
        picks = [p.strip().lower() for p in line.split(",")] if choices else []
        if choices and picks and all(len(p) == 1 and 0 <= ord(p) - 97 < len(choices) for p in picks):
            chosen = [choices[ord(p) - 97] for p in picks]
            out.append(chosen if q.get("multi_select") else chosen[0])
        else:
            out.append(line)
    return out


class Bridge:
    """One bot, one paired account. *on_message(text)* gets what you write; the app calls *send*,
    *progress*, *ask_approval* and *ask_text* to reach you."""

    def __init__(self, home: Path, on_message: Callable[[str], Awaitable[None]],
                 on_command: Callable[[str], Awaitable[str]], log: Callable[[str, str], None],
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.home = home
        self.on_message, self.on_command, self.log = on_message, on_command, log
        self.state = load_state(home)
        self.token = token(home)
        self.client = httpx.AsyncClient(base_url=f"{API}/bot{self.token}/", transport=transport or TRANSPORT,
                                        timeout=httpx.Timeout(connect=15, read=70, write=30, pool=15))
        self.username = ""
        self.pair_code = ""
        self.task: Optional[asyncio.Task] = None
        self.active = False
        self._approvals: dict[str, asyncio.Future] = {}
        self._answer: Optional[asyncio.Future] = None
        self._progress_msg: Optional[int] = None
        self._progress: list[str] = []
        self._last_edit = 0.0
        self._typing_task: Optional[asyncio.Task] = None

    # ── Telegram API ──────────────────────────────────────────────────
    async def call(self, method: str, **params: Any) -> Any:
        r = await self.client.post(method, json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', r.status_code)}")
        return data.get("result")

    @property
    def owner(self) -> Optional[int]:
        v = self.state.get("owner_id")
        return int(v) if v else None

    @property
    def chat(self) -> Optional[int]:
        v = self.state.get("chat_id")
        return int(v) if v else None

    async def start(self) -> str:
        """Check the token, then listen. Returns the bot's @name."""
        me = await self.call("getMe")
        self.username = str(me.get("username") or "")
        if not self.owner:
            self.pair_code = f"{secrets.randbelow(10 ** 6):06d}"
        self.active = True
        self.task = asyncio.create_task(self._poll())
        if self.chat:
            await self.send("🟢 Hermes Lite is listening. /help shows what you can do from here.")
        return self.username

    async def stop(self, note: str = "⚪ Hermes Lite stopped listening here.") -> None:
        if self.active and self.chat and note:
            try:
                await self.send(note)
            except Exception:
                pass
        self.active = False
        for fut in list(self._approvals.values()) + ([self._answer] if self._answer else []):
            if not fut.done():
                fut.cancel()
        if self.task:
            self.task.cancel()
        if self._typing_task:
            self._typing_task.cancel()
        await self.client.aclose()

    async def _poll(self) -> None:
        offset = int(self.state.get("offset") or 0)
        while self.active:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=50,
                                          allowed_updates=["message", "callback_query"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"Telegram: {exc} — retrying in 10 s.", "error")
                await asyncio.sleep(10)
                continue
            for u in updates or []:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                try:
                    await self._handle(u)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one odd update must not stop the bridge
                    self.log(f"Telegram: could not handle a message ({exc}).", "error")
            if updates:
                self.state["offset"] = offset
                save_state(self.home, {**load_state(self.home), "offset": offset})

    # ── incoming ──────────────────────────────────────────────────────
    async def _handle(self, u: dict) -> None:
        if cq := u.get("callback_query"):
            if self.owner and int(cq.get("from", {}).get("id", 0)) == self.owner:
                await self._callback(cq)
            return
        msg = u.get("message") or {}
        sender = int(msg.get("from", {}).get("id", 0))
        text = str(msg.get("text") or msg.get("caption") or "").strip()
        if not self.owner:
            if self.pair_code and text == self.pair_code:
                self.state.update(owner_id=sender, chat_id=int(msg["chat"]["id"]))
                save_state(self.home, {**load_state(self.home), "owner_id": sender, "chat_id": self.state["chat_id"]})
                self.pair_code = ""
                self.log(f"Telegram: paired with {msg.get('from', {}).get('username') or sender}. Only this "
                         "account is answered from now on.", "ok")
                await self.send("✅ Paired. Write to me here as you would in Hermes Lite. /help for the commands.")
            return                       # strangers get no reply at all
        if sender != self.owner:
            return
        if not text:
            await self.send("Only text for now (send a file's path on the computer instead).")
            return
        if self._answer and not self._answer.done() and not text.lower().startswith("/stop"):
            self._answer.set_result(text)
            return
        if text.startswith("/"):
            await self.send(await self.on_command(text.split()[0][1:].split("@")[0].lower()))
            return
        await self.on_message(text)

    async def _callback(self, cq: dict) -> None:
        kind, _, rest = str(cq.get("data") or "").partition(":")
        key, _, value = rest.partition(":")
        fut = self._approvals.get(key) if kind == "ap" else None
        try:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])
        except Exception:
            pass
        if fut and not fut.done():
            fut.set_result(value)

    # ── outgoing ──────────────────────────────────────────────────────
    async def send(self, text: str, markdown: bool = False, **extra: Any) -> Optional[int]:
        if not self.chat:
            return None
        msg_id = None
        for part in chunks(text):
            try:
                if markdown:
                    res = await self.call("sendMessage", chat_id=self.chat, text=to_telegram_markdown(part),
                                          parse_mode="Markdown", disable_web_page_preview=True, **extra)
                else:
                    raise ValueError
            except Exception:  # plain text when Telegram refuses the Markdown
                res = await self.call("sendMessage", chat_id=self.chat, text=part, disable_web_page_preview=True,
                                      **extra)
            msg_id = res.get("message_id") if isinstance(res, dict) else msg_id
        return msg_id

    async def send_photo(self, path: str, caption: str = "") -> None:
        if not self.chat:
            return
        with open(path, "rb") as f:
            r = await self.client.post("sendPhoto", data={"chat_id": str(self.chat), "caption": caption[:1000]},
                                       files={"photo": (Path(path).name, f.read())})
        if not r.json().get("ok"):
            raise RuntimeError(f"Telegram sendPhoto: {r.json().get('description')}")

    def typing(self, on: bool) -> None:
        """'typing…' at the top of the chat while Hermes Lite works."""
        if self._typing_task:
            self._typing_task.cancel()
            self._typing_task = None
        if on and self.chat:
            async def keep():
                while True:
                    try:
                        await self.call("sendChatAction", chat_id=self.chat, action="typing")
                    except Exception:
                        pass
                    await asyncio.sleep(4.5)
            self._typing_task = asyncio.create_task(keep())

    async def progress(self, line: str) -> None:
        """One message per request, edited as tools run, instead of a message per tool call."""
        if not self.chat:
            return
        self._progress.append(line[:200])
        text = "⏳ Working…\n" + "\n".join(self._progress[-PROGRESS_LINES:])
        if self._progress_msg is None:
            self._progress_msg = await self.send(text)
            self._last_edit = time.monotonic()
        elif time.monotonic() - self._last_edit >= EDIT_EVERY:
            await self._edit_progress(text)

    async def _edit_progress(self, text: str) -> None:
        try:
            await self.call("editMessageText", chat_id=self.chat, message_id=self._progress_msg, text=text[:LIMIT])
        except Exception:
            pass
        self._last_edit = time.monotonic()

    async def progress_done(self, note: str) -> None:
        if self._progress_msg is not None:
            steps = len(self._progress)
            await self._edit_progress(f"{note} · {steps} step{'s' if steps != 1 else ''}\n"
                                      + "\n".join(self._progress[-PROGRESS_LINES:]))
        self._progress_msg, self._progress = None, []

    async def ask_approval(self, command: str, reason: str, fut: asyncio.Future) -> Optional[int]:
        """Approve / deny buttons; *fut* gets once | session | deny when one is tapped."""
        key = secrets.token_hex(4)
        self._approvals[key] = fut
        fut.add_done_callback(lambda _: self._approvals.pop(key, None))
        buttons = [[{"text": "Allow once", "callback_data": f"ap:{key}:once"},
                    {"text": "Allow for session", "callback_data": f"ap:{key}:session"}],
                   [{"text": "Deny", "callback_data": f"ap:{key}:deny"}]]
        return await self.send(f"⚠ Run this command? ({reason})\n\n{command[:3000]}",
                               reply_markup={"inline_keyboard": buttons})

    async def close_approval(self, msg_id: Optional[int], verdict: str) -> None:
        if msg_id is None:
            return
        word = {"once": "✅ allowed once", "session": "✅ allowed for this session"}.get(verdict, "⛔ denied")
        try:
            await self.call("editMessageReplyMarkup", chat_id=self.chat, message_id=msg_id,
                            reply_markup={"inline_keyboard": []})
            await self.send(word)
        except Exception:
            pass

    def expect_answer(self) -> asyncio.Future:
        """Your next message answers the question asked (instead of starting a new request)."""
        self._answer = asyncio.get_running_loop().create_future()
        return self._answer


async def pair_in_terminal(home: Path) -> bool:
    """For hermes-lite --telegram: show a pairing code in the terminal and wait for it."""
    done = asyncio.Event()

    async def ignore(_: str) -> None:
        pass

    async def no_cmd(_: str) -> str:
        return ""

    def log(text: str, tone: str) -> None:
        print(text)
        if text.startswith("Telegram: paired"):
            done.set()
    b = Bridge(home, ignore, no_cmd, log)
    name = await b.start()
    print(f"Send this code to @{name} on Telegram to pair it with your account: {b.pair_code}")
    try:
        await asyncio.wait_for(done.wait(), timeout=600)
        return True
    except asyncio.TimeoutError:
        print("No pairing code received within 10 minutes.")
        return False
    finally:
        await b.stop(note="")
