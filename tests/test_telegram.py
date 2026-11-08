"""Telegram: pairing, only your account, approvals as buttons, replies back, no sudo over chat."""
import asyncio
import json
import stat

import httpx

from hermes_lite import telegram as tg

TOKEN = "123456789:" + "A" * 35


class FakeTelegram:
    """Enough of the Bot API: getUpdates hands out queued updates; every call is recorded."""

    def __init__(self):
        self.updates, self.calls, self.n = [], [], 0
        self.refuse_markdown = False

    def update(self, **u):
        self.n += 1
        self.updates.append({"update_id": self.n, **u})

    def msg(self, sender, text, chat=None):
        self.update(message={"from": {"id": sender, "username": f"u{sender}"}, "chat": {"id": chat or sender},
                             "text": text})

    def sent(self, method="sendMessage"):
        return [c for m, c in self.calls if m == method]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else {}
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "hermes_test_bot"}})
        if method == "getUpdates":
            if not self.updates:
                await asyncio.sleep(0.02)
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage" and body.get("parse_mode") and self.refuse_markdown:
            return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.calls)}})

    def transport(self):
        return httpx.MockTransport(self.handler)


async def _settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0.03)


def test_pairing_and_only_the_owner(tmp_path):
    tg.save_state(tmp_path, {"token": TOKEN})
    fake, got, cmds = FakeTelegram(), [], []

    async def on_message(text):
        got.append(text)

    async def on_command(cmd):
        cmds.append(cmd)
        return f"ran {cmd}"

    async def go():
        b = tg.Bridge(tmp_path, on_message, on_command, lambda t, k: None, transport=fake.transport())
        assert await b.start() == "hermes_test_bot" and len(b.pair_code) == 6
        fake.msg(666, "hello")                          # a stranger, before pairing
        fake.msg(666, "000000" if b.pair_code != "000000" else "111111")
        await _settle()
        assert not fake.sent() and not got              # no reply at all
        fake.msg(42, b.pair_code)
        await _settle()
        assert b.owner == 42 and "Paired" in fake.sent()[-1]["text"]
        fake.msg(666, "rm -rf /")                       # still a stranger
        fake.msg(42, "build the player")
        fake.msg(42, "/status@hermes_test_bot")
        await _settle()
        await b.stop()
        return b
    asyncio.run(go())
    assert got == ["build the player"] and cmds == ["status"]
    state = tg.load_state(tmp_path)
    assert state["owner_id"] == 42 and state["chat_id"] == 42 and state["token"] == TOKEN
    assert stat.S_IMODE(tg.state_path(tmp_path).stat().st_mode) == 0o600


def test_approval_buttons_and_long_replies(tmp_path):
    tg.save_state(tmp_path, {"token": TOKEN, "owner_id": 42, "chat_id": 42})
    fake = FakeTelegram()
    fake.refuse_markdown = True

    async def nothing(*a):
        return ""

    async def go():
        b = tg.Bridge(tmp_path, nothing, nothing, lambda t, k: None, transport=fake.transport())
        await b.start()
        fut = asyncio.get_running_loop().create_future()
        await b.ask_approval("rm -rf build", "recursive delete", fut)
        key = fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
        fake.update(callback_query={"id": "x", "from": {"id": 666}, "data": key})    # not the owner
        await _settle()
        assert not fut.done()
        fake.update(callback_query={"id": "y", "from": {"id": 42}, "data": key})
        await _settle()
        assert fut.result() == "session"
        await b.send("**Done**\n" + "line\n" * 2000, markdown=True)                  # ~10k characters
        await b.stop()
    asyncio.run(go())
    parts = [c for c in fake.sent() if c["text"].startswith(("*Done*", "**Done**", "line"))]
    plain = [c for c in parts if "parse_mode" not in c]   # Markdown refused: each part sent again as plain text
    assert len(plain) >= 3 and len(plain) == len(parts) - len(plain)
    assert all(len(c["text"]) <= tg.LIMIT for c in parts)


def test_markdown_and_answers():
    assert tg.to_telegram_markdown("## Plan\n**bold** and `x`\n```\n**kept**\n```") == \
        "*Plan*\n*bold* and `x`\n```\n**kept**\n```"
    qs = [{"question": "Which db?", "choices": ["sqlite", "postgres"]},
          {"question": "Features?", "choices": ["auth", "search", "export"], "multi_select": True},
          {"question": "Name?"}]
    assert tg.parse_answers("b\n1) a,c\nplayer", qs) == ["postgres", ["auth", "export"], "player"]
    assert tg.parse_answers("use mysql", qs[:1]) == ["use mysql"]
    assert "a) sqlite" in tg.format_questions(qs)


def test_session_reachable_from_the_phone(tmp_path, monkeypatch):
    """/telegram on inside the TUI: a phone message runs the agent, the answer and the approval go
    to the phone, the computer's dialog closes, and sudo is refused for a remote request."""
    from hermes_lite.config import load_config
    from hermes_lite.llm import ToolCall, Turn
    from hermes_lite.tui.app import HermesApp
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_LITE_HOME", str(home))
    home.mkdir()
    tg.save_state(home, {"token": TOKEN, "owner_id": 42, "chat_id": 42})
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "TRANSPORT", fake.transport())
    cfg = load_config(str(tmp_path / "none.yaml"))

    async def go():
        app = HermesApp(cfg, cwd=str(tmp_path), start_server=False)
        async with app.run_test(size=(140, 40)) as pilot:
            app.server_ready.set()
            turns = iter([Turn(tool_calls=[ToolCall(id="t1", name="terminal",
                                                    arguments='{"command": "rm -rf build"}',
                                                    parsed={"command": "rm -rf build"})]),
                          Turn(content="**Cleaned** the build folder.")])

            async def stream(messages, tools, **kw):
                return next(turns)
            app.agent.llm.stream = stream
            app.agent.llm.vision = False
            app.agent.llm.probe_vision = lambda: asyncio.sleep(0, result=False)
            await app._command("/telegram on")
            await _settle()
            fake.msg(42, "clean the build folder")
            for _ in range(60):
                await asyncio.sleep(0.05)
                buttons = [c for c in fake.sent() if c.get("reply_markup", {}).get("inline_keyboard")]
                if buttons:
                    break
            assert buttons, "no approval buttons on the phone"
            assert type(app.screen).__name__ == "ApprovalScreen"         # and the same question at the computer
            key = buttons[-1]["reply_markup"]["inline_keyboard"][1][0]["callback_data"]   # Deny
            fake.update(callback_query={"id": "z", "from": {"id": 42}, "data": key})
            for _ in range(60):
                await asyncio.sleep(0.05)
                if any("Cleaned" in c["text"] for c in fake.sent()):
                    break
            assert type(app.screen).__name__ != "ApprovalScreen"          # closed at the computer too
            texts = [c["text"] for c in fake.sent()]
            assert any(t.startswith("*Cleaned*") for t in texts) and any("denied" in t for t in texts)
            assert any("Working" in c.get("text", "") for _, c in fake.calls)
            # a remote request never gets a sudo prompt
            assert app._remote_run
            assert await app.ask_secret("[sudo] password:", "sudo pacman -S foo") == (None, False)
            assert "never typed" not in texts[-1] and "only ever typed at the computer" in fake.sent()[-1]["text"]
            await app._command("/telegram off")
    asyncio.run(go())


def test_pairing_from_the_terminal(tmp_path, monkeypatch, capsys):
    """hermes-lite --telegram with no paired account: the code is printed, the owner saved."""
    tg.save_state(tmp_path, {"token": TOKEN})
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "TRANSPORT", fake.transport())
    monkeypatch.setattr(tg.secrets, "randbelow", lambda n: 123456)

    async def go():
        task = asyncio.create_task(tg.pair_in_terminal(tmp_path))
        await _settle()
        fake.msg(7, "123456")
        return await asyncio.wait_for(task, 5)
    assert asyncio.run(go()) is True
    assert "123456" in capsys.readouterr().out and tg.load_state(tmp_path)["owner_id"] == 7


def test_token_box_can_be_typed_into(tmp_path, monkeypatch):
    """/telegram on typed in the prompt with no token yet: the token box must take input (it used to
    freeze the whole app: the command waited for the box inside the input handler)."""
    from hermes_lite.config import load_config
    from hermes_lite.tui.app import HermesApp
    from hermes_lite.tui.widgets import PromptInput
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_LITE_HOME", str(home))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "TRANSPORT", fake.transport())

    async def go():
        app = HermesApp(load_config(str(tmp_path / "none.yaml")), cwd=str(tmp_path), start_server=False)
        async with app.run_test(size=(140, 40)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.focus()
            prompt.text = "/telegram on"
            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause(0.05)
                if type(app.screen).__name__ == "SecretScreen":
                    break
            assert type(app.screen).__name__ == "SecretScreen"
            await pilot.press(*TOKEN)
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if app.telegram and app.telegram.active:
                    break
            assert app.telegram and app.telegram.active and app.telegram.pair_code
            await app._command("/telegram off")
    asyncio.run(asyncio.wait_for(go(), 30))
    assert tg.load_state(home)["token"] == TOKEN
