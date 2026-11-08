"""Context keeping: thinking is not counted, old tool output is trimmed before summarizing, and the
compaction handoff carries facts recorded by code."""
import asyncio
import json

from hermes_lite import compaction
from hermes_lite.agent import Agent
from hermes_lite.config import load_config
from hermes_lite.llm import Turn
from hermes_lite.worklog import WorkLog


class UI:
    def __init__(self):
        self.events = []

    def event(self, kind, **data):
        self.events.append((kind, data))


def _agent(tmp_path, ctx=40000):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg["model"]["context_length"] = ctx
    return Agent(cfg, UI(), cwd=str(tmp_path))


def test_thinking_is_not_counted():
    msgs = [{"role": "assistant", "content": "ok", "reasoning_content": "x" * 32000}]
    assert compaction.estimate_tokens(msgs) < 10
    assert compaction.estimate_tokens(msgs, reasoning=True) >= 10000


def test_limit_leaves_room_for_the_reply(tmp_path):
    a = _agent(tmp_path, 40000)
    limit, keep = a._limits(40000)
    assert limit <= 40000 - 8192 - 2048          # thinking budget + answer fit after the prompt
    assert limit > 20000                         # no longer half the context
    assert 1500 <= keep <= 10000
    big = _agent(tmp_path, 64000)._limits(64000)
    assert big[0] == int(0.8 * 64000) and big[1] == 16000


def test_measured_prompt_plus_new_messages(tmp_path):
    a = _agent(tmp_path)
    a.messages.append({"role": "user", "content": "hi"})
    a._measured = (12000, len(a.messages), a.messages[-1])
    a.messages.append({"role": "assistant", "content": "", "reasoning_content": "y" * 30000})
    a.messages.append({"role": "tool", "tool_call_id": "1", "name": "terminal", "content": "z" * 3200})
    assert 12900 < a._context_tokens() < 13200   # 12000 + ~1000 for the tool result, thinking ignored
    a.messages.pop(); a.messages.pop(); a.messages.pop()   # /undo: the measurement no longer applies
    assert a._context_tokens() == a._estimate()


def _conversation(a, steps=12):
    a.messages.append({"role": "user", "content": "Build the music player. Keep the UI in Qt please."})
    for i in range(steps):
        cid = f"c{i}"
        body = "\n".join(f"line {n} of file {i}" for n in range(400))
        name, args, result = ("read_file", {"path": f"src/m{i}.py"},
                              json.dumps({"path": f"/p/src/m{i}.py", "total_lines": 400, "content": body}))
        if i % 3 == 2:
            name, args, result = "write_file", {"path": f"src/n{i}.py", "content": body}, json.dumps(
                {"success": True, "path": f"/p/src/n{i}.py", "created": True})
        a.messages.append({"role": "assistant", "content": f"Step {i}: looking at module {i}.",
                           "tool_calls": [{"id": cid, "type": "function",
                                           "function": {"name": name, "arguments": json.dumps(args)}}]})
        a.messages.append({"role": "tool", "tool_call_id": cid, "name": name, "content": result})


def test_prune_keeps_messages_and_calls(tmp_path):
    a = _agent(tmp_path)
    _conversation(a)
    before = a._estimate()
    limit, keep = a._limits(40000)
    items, freed = a._prune(keep, limit)
    assert items > 0 and freed > 0 and a._estimate() < before
    # every message is still there, with its words and its tool call
    assert len(a.messages) == 2 + 24
    assert a.messages[1]["content"].startswith("Build the music player")
    first_read = a.messages[3]
    assert first_read.get("_pruned") and "read_file the part you need" in first_read["content"]
    assert json.loads(first_read["content"])["path"] == "/p/src/m0.py"
    write_call = a.messages[6]["tool_calls"][0]["function"]
    assert json.loads(write_call["arguments"])["path"] == "src/n2.py"
    assert "removed from the conversation" in json.loads(write_call["arguments"])["content"]
    # the recent part is untouched
    assert not a.messages[-3].get("_pruned") and len(a.messages[-3]["content"]) > 1500


def test_compact_prunes_without_summary(tmp_path):
    a = _agent(tmp_path)
    _conversation(a, steps=14)

    async def no_llm(*args, **kw):
        raise AssertionError("the summary should not be needed")
    a.llm.stream = no_llm
    a.save = lambda: None
    assert asyncio.run(a.compact()) is True
    assert any(k == "pruned" for k, _ in a.ui.events)
    assert a._estimate() < a._limits(40000)[0] * 0.8


def test_compact_summary_carries_facts(tmp_path):
    a = _agent(tmp_path)
    a.cfg["agent"]["prune_old_outputs"] = False
    (tmp_path / "app.py").write_text("a\nb\nc\n")
    a.worklog.record("write_file", {"path": "app.py", "content": "a"},
                     json.dumps({"success": True, "path": str(tmp_path / "app.py"), "created": True}), str(tmp_path))
    a.worklog.record("terminal", {"command": "pytest -q"}, json.dumps({"output": "1 failed", "exit_code": 1}), str(tmp_path))
    _conversation(a, steps=14)

    async def fake(messages, tools, **kw):
        return Turn(content="## Goal\nmusic player")
    a.llm.stream = fake
    a.save = lambda: None
    assert asyncio.run(a.compact()) is True
    handoff = a.messages[1]["content"]
    assert "## Goal" in handoff
    assert "Build the music player. Keep the UI in Qt please." in handoff
    assert "app.py — created (now 3 lines)" in handoff
    assert "`pytest -q` → exit 1" in handoff
    assert "Your last message before the compaction" in handoff


def test_worklog_roundtrip_and_budget(tmp_path):
    log = WorkLog()
    log.add_requests([{"role": "user", "content": f"request {i} " + "x" * 1400} for i in range(12)]
                     + [{"role": "user", "content": "nudge", "_synthetic": True}])
    text = log.render(str(tmp_path))
    assert "1. request 0" in text and "12. request 11" in text and "earlier messages left out" in text
    assert "nudge" not in text and len(text) < 7500
    assert WorkLog.from_dict(log.to_dict()).requests == log.requests


def test_request_in_progress_survives_compactions(tmp_path):
    a = _agent(tmp_path)
    a.cfg["agent"]["prune_old_outputs"] = False
    _conversation(a, steps=14)          # the only user message is at the start: it gets compacted

    async def fake(messages, tools, **kw):
        return Turn(content="## Goal\nmusic player")
    a.llm.stream = fake
    a.save = lambda: None
    assert asyncio.run(a.compact()) is True
    handoff = a.messages[1]["content"]
    assert "The request you are working on now — NOT finished" in handoff
    assert handoff.rstrip().endswith("Build the music player. Keep the UI in Qt please.")
    # more work, then a second compaction: the request is no longer in the messages at all
    for i in range(14, 30):
        a.messages.append({"role": "assistant", "content": f"Step {i}.", "tool_calls": [{"id": f"d{i}", "type": "function",
                           "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}]})
        a.messages.append({"role": "tool", "tool_call_id": f"d{i}", "name": "terminal", "content": "x" * 4000})
    assert asyncio.run(a.compact()) is True
    assert a.messages[1]["content"].rstrip().endswith("Build the music player. Keep the UI in Qt please.")
    # a new message from the user replaces it
    a.messages.append({"role": "user", "content": "now add a volume slider"})
    for i in range(30, 46):
        a.messages.append({"role": "assistant", "content": f"Step {i}.", "tool_calls": [{"id": f"d{i}", "type": "function",
                           "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}]})
        a.messages.append({"role": "tool", "tool_call_id": f"d{i}", "name": "terminal", "content": "x" * 4000})
    assert asyncio.run(a.compact()) is True
    handoff = a.messages[1]["content"]
    assert handoff.rstrip().endswith("now add a volume slider")           # the new request replaces it
    assert "Build the music player" not in handoff.split("NOT finished")[1]



def _read(a, cid, path, lines=400, **args):
    body = "\n".join(f"{n}|code line {n} of {path}" for n in range(1, lines + 1))
    a.messages.append({"role": "assistant", "content": f"Reading {path}.", "tool_calls": [{"id": cid, "type": "function",
                       "function": {"name": "read_file", "arguments": json.dumps({"path": path, **args})}}]})
    a.messages.append({"role": "tool", "tool_call_id": cid, "name": "read_file",
                       "content": json.dumps({"path": "/p/" + path, "total_lines": lines, "content": body})})


def _noise(a, n, size=6000):
    for i in range(n):
        a.messages.append({"role": "assistant", "content": f"Run {i}.", "tool_calls": [{"id": f"t{len(a.messages)}",
                           "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"make\"}"}}]})
        a.messages.append({"role": "tool", "tool_call_id": f"t{len(a.messages) - 1}", "name": "terminal",
                           "content": "build output " * (size // 13)})


def _content(a, cid):
    return next(m for m in a.messages if m.get("tool_call_id") == cid)["content"]


def test_latest_reads_stay_whole(tmp_path):
    a = _agent(tmp_path)
    a.messages.append({"role": "user", "content": "fix the player"})
    _read(a, "old1", "a.py"); _read(a, "old2", "b.py")
    _read(a, "r1", "player.py", 150); _read(a, "r2", "ui.py", 150); _read(a, "r3", "db.py", 150)
    _noise(a, 12)                              # pushes all reads out of the recent part
    limit, keep = a._limits(40000)
    a._prune(keep, limit)
    for cid in ("r1", "r2", "r3"):
        assert "code line 150" in _content(a, cid)            # the three latest: whole
    for cid in ("old1", "old2"):
        assert "read_file the part you need" in _content(a, cid)  # earlier ones: trimmed


def test_reread_supersedes_and_unchanged_points_back(tmp_path):
    a = _agent(tmp_path)
    a.messages.append({"role": "user", "content": "go"})
    _read(a, "first", "main.py")
    _read(a, "other", "util.py", 100)
    _read(a, "again", "main.py")               # same file and range, read again
    # an "unchanged" reply points back to the earlier result, which must stay
    a.messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "same", "type": "function",
                       "function": {"name": "read_file", "arguments": "{\"path\": \"util.py\"}"}}]})
    a.messages.append({"role": "tool", "tool_call_id": "same", "name": "read_file",
                       "content": json.dumps({"path": "/p/util.py", "unchanged": True})})
    limit, keep = a._limits(40000)
    a._prune(keep, limit)
    assert "read_file the part you need" in _content(a, "first")   # superseded, even though recent
    assert "code line 400" in _content(a, "again")
    assert "code line 100" in _content(a, "other")


def test_summary_keeps_latest_read_in_tail(tmp_path):
    a = _agent(tmp_path)
    a.cfg["agent"]["prune_old_outputs"] = False
    a.messages.append({"role": "user", "content": "go"})
    _noise(a, 6)
    _read(a, "key", "player.py", 200)
    _noise(a, 8, size=3000)

    async def fake(messages, tools, **kw):
        return Turn(content="## Goal\nx")
    a.llm.stream = fake
    a.save = lambda: None
    assert asyncio.run(a.compact()) is True
    assert "code line 200" in _content(a, "key")
    i = next(i for i, m in enumerate(a.messages) if m.get("tool_call_id") == "key")
    assert a.messages[i - 1]["tool_calls"][0]["id"] == "key"          # its call is kept with it


def test_esc_after_an_image_never_sends_it_again(tmp_path, monkeypatch):
    from hermes_lite.llm import ToolCall
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    a = _agent(tmp_path)
    a.llm.vision = True
    img = tmp_path / "shot.png"
    from PIL import Image
    Image.new("RGB", (8, 8)).save(img)
    turns = iter([Turn(tool_calls=[ToolCall(id="v1", name="view_image", arguments=json.dumps({"path": str(img)}),
                                            parsed={"path": str(img)})]),
                  Turn(content="I see a black squ", interrupted=True)])
    sent = []

    async def fake(messages, tools, **kw):
        sent.append(messages)
        return next(turns)
    a.llm.stream = fake
    a.save = lambda: None
    asyncio.run(a.run("look at shot.png"))
    assert any(isinstance(m.get("content"), list) for m in sent[1])          # shown once
    assert not any(m.get("_images") for m in a.messages)                     # then retired, despite Esc
    note = next(m["content"] for m in a.messages if "removed to save context" in str(m.get("content")))
    assert "I see a black squ" in note
