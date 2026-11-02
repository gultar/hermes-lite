"""The model's own notebook (notes tool) and the project's todo list, across sessions."""
import asyncio
import json

from hermes_lite.agent import Agent
from hermes_lite.config import load_config
from hermes_lite.llm import ToolCall, Turn
from hermes_lite.tools import REGISTRY, notes


class UI:
    def event(self, *a, **k):
        pass


def _agent(tmp_path, monkeypatch, cwd=None):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "Cargo.toml").write_text("[package]\nname='blog_os'\n")
    return Agent(load_config(str(tmp_path / "none.yaml")), UI(), cwd=str(cwd or proj)), proj


def _call(a, name, **args):
    c = ToolCall(id="n1", name=name, arguments=json.dumps(args))
    c.parsed = args
    return json.loads(a._run_tool_sync(c))


def test_add_update_remove_and_limits(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    r = _call(a, "notes", text="Build with: cargo bootimage; run with qemu-system-x86_64 -drive format=raw,file=…")
    assert r["added"] == 1 and (proj / ".hermes" / "notes.md").is_file()
    _call(a, "notes", text="VGA buffer is at 0xb8000 (src/vga_buffer.rs:12)")
    assert _call(a, "notes", text="vga buffer is at 0xb8000 (src/vga_buffer.rs:12)")["note"] == "Already noted."
    _call(a, "notes", action="update", id=2, text="VGA buffer at 0xb8000, wrapped in Volatile (src/vga_buffer.rs:12)")
    _call(a, "notes", action="remove", id=1)
    listing = _call(a, "notes", action="list")["notes"]
    assert listing == "[2] VGA buffer at 0xb8000, wrapped in Volatile (src/vga_buffer.rs:12)"
    assert "Too long" in _call(a, "notes", text="x" * 400)["error"]


def test_notes_and_open_todos_come_back_next_session(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    _call(a, "notes", text="Chapter 3 (VGA text mode) done; next: chapter 4, testing")
    _call(a, "todo_list", todos=[{"id": "1", "content": "Read chapter 4", "status": "in_progress"},
                                 {"id": "2", "content": "Add integration tests", "status": "pending"},
                                 {"id": "0", "content": "Chapter 3", "status": "completed"}])
    b, _ = _agent(tmp_path, monkeypatch)   # a new session in the same project
    prompt = b.messages[0]["content"]
    assert "# Your working notes for this project" in prompt and "next: chapter 4, testing" in prompt
    assert "- [in_progress] Read chapter 4" in prompt and "Chapter 3" not in prompt.split("Unfinished todo")[1]
    assert [t["content"] for t in b.ctx.state["todos"]][:2] == ["Read chapter 4", "Add integration tests"]


def test_finished_lists_do_not_come_back(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    _call(a, "todo_list", todos=[{"id": "1", "content": "Done thing", "status": "completed"}])
    b, _ = _agent(tmp_path, monkeypatch)
    assert not b.ctx.state.get("todos") and "Unfinished todo" not in b.messages[0]["content"]


def test_notes_are_back_after_compaction(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    a.cfg["agent"]["prune_old_outputs"] = False
    a.messages.append({"role": "user", "content": "go"})
    for i in range(12):
        a.messages.append({"role": "assistant", "content": f"step {i}", "tool_calls": [{"id": f"t{i}", "type": "function",
                           "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}]})
        a.messages.append({"role": "tool", "tool_call_id": f"t{i}", "name": "terminal", "content": "y" * 6000})
    _call(a, "notes", text="The panic handler lives in src/main.rs:40")

    async def fake(messages, tools, **kw):
        return Turn(content="## Goal\n" + "summary " * 60)
    a.llm.stream = fake
    a.save = lambda: None
    assert asyncio.run(a.compact()) is True
    assert "The panic handler lives in src/main.rs:40" in a.messages[0]["content"]
