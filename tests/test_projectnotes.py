"""Project notes: read at session start, written by the model at the end of the cached conversation."""
import asyncio
import json

from hermes_lite import projectnotes
from hermes_lite.agent import Agent
from hermes_lite.config import load_config
from hermes_lite.llm import Turn

NOTES = """# Music player
## What it is
A Qt music player.
## Layout
- src/main.py — entry point
## Done so far
- 2026-10-29: playlist view
## Current state
Works.
## Next
Volume slider.
## Notes
Run with python -m player."""


class UI:
    def __init__(self):
        self.events = []

    def event(self, kind, **data):
        self.events.append((kind, data))


def _agent(tmp_path, monkeypatch, project=True):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    if project:
        (proj / "pyproject.toml").write_text("[project]\nname='x'\n")
    return Agent(load_config(str(tmp_path / "none.yaml")), UI(), cwd=str(proj)), proj


def test_clean_accepts_notes_and_rejects_chatter():
    assert projectnotes.clean("```markdown\n" + NOTES + "\n```") == NOTES
    assert projectnotes.clean("Sure! I updated the notes.") is None


def test_save_keeps_history_and_load_strips_header(tmp_path):
    path = tmp_path / "PROJECT_NOTES.md"
    projectnotes.save(path, NOTES)
    projectnotes.save(path, NOTES.replace("Volume slider.", "Equalizer."))
    assert projectnotes.load(path).endswith("Run with python -m player.") and "Equalizer." in projectnotes.load(path)
    assert len(list((tmp_path / ".hermes" / "notes-history").glob("project-*.md"))) == 1


def test_notes_are_in_the_system_prompt_only_in_projects(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    projectnotes.save(proj / "PROJECT_NOTES.md", NOTES)
    a2 = Agent(a.cfg, UI(), cwd=str(proj))
    assert "your memory of this project" in a2.messages[0]["content"] and "Volume slider." in a2.messages[0]["content"]
    other = tmp_path / "loose"
    other.mkdir()
    assert projectnotes.notes_path(a.cfg, other) is None


def test_update_writes_the_file_from_the_cached_conversation(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    a.messages.append({"role": "user", "content": "add a playlist view"})
    a.iters_since_notes = 9
    seen = {}

    async def fake(messages, tools, **kw):
        seen["last"] = messages[-1]["content"]
        seen["tools"] = len(tools)
        seen["prefix_same"] = messages[:-1] == a._wire_messages()
        return Turn(content=NOTES)
    a.llm.stream = fake
    assert asyncio.run(a.update_project_notes("auto")) == "saved"
    assert projectnotes.load(proj / "PROJECT_NOTES.md") == NOTES
    assert seen["last"].startswith("[Hermes Lite: update the project notes]")
    assert seen["tools"] == len(a.tools_schema()) and seen["prefix_same"]   # llama-server's cache is reused
    assert a.iters_since_notes == 0
    assert ("notes_end", {"outcome": "saved", "path": str(proj / "PROJECT_NOTES.md"),
            "detail": f"{len(NOTES.split())} words"}) in a.ui.events


def test_a_bad_reply_changes_nothing(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    a.iters_since_notes = 9

    async def fake(messages, tools, **kw):
        return Turn(content="I will update the notes now.")
    a.llm.stream = fake
    assert asyncio.run(a.update_project_notes("auto")) == "failed"
    assert not (proj / "PROJECT_NOTES.md").exists() and a.iters_since_notes == 9



def test_old_location_is_moved_to_the_root(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    (proj / ".hermes").mkdir()
    (proj / ".hermes" / "project.md").write_text(NOTES)
    path = projectnotes.notes_path(a.cfg, proj)
    assert path == proj / "PROJECT_NOTES.md" and projectnotes.load(path) == NOTES
    assert not (proj / ".hermes" / "project.md").exists()


def test_first_version_comes_early(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    a.cfg["skills"]["auto_review"] = False
    calls = []

    async def fake(messages, tools, **kw):
        calls.append(1)
        return Turn(content=NOTES)
    a.llm.stream = fake
    a.iters_since_notes = 3
    asyncio.run(a._maybe_review(40000))
    assert calls and (proj / "PROJECT_NOTES.md").exists()        # first version after 3 tool calls
    a.iters_since_notes = 3
    asyncio.run(a._maybe_review(40000))
    assert len(calls) == 1                                       # afterwards: every 8


def test_merge_appends_done_and_replaces_changed_sections():
    delta = """## Done so far
- 2026-10-30: volume slider
- 2026-10-29: playlist view
## Next
Equalizer."""
    merged = projectnotes.merge(NOTES, delta)
    assert merged.count("playlist view") == 1                       # an existing bullet is not repeated
    assert merged.index("playlist view") < merged.index("volume slider")
    assert "Equalizer." in merged and "Volume slider." not in merged  # 'Next' replaced
    assert "Run with python -m player." in merged and merged.startswith("# Music player")  # the rest kept
    assert projectnotes.merge(NOTES, "NO CHANGES") is None
    assert projectnotes.merge(NOTES, "## Next\nVolume slider.") is None


def test_later_updates_send_only_the_changes(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    projectnotes.save(proj / "PROJECT_NOTES.md", NOTES)
    a.iters_since_notes = 9
    seen = {}

    async def fake(messages, tools, **kw):
        seen["request"], seen["max_tokens"] = messages[-1]["content"], kw.get("max_tokens")
        return Turn(content="## Done so far\n- 2026-10-30: volume slider")
    a.llm.stream = fake
    assert asyncio.run(a.update_project_notes("auto")) == "saved"
    assert seen["request"].startswith("[Hermes Lite: update the project notes — changes only]")
    assert seen["max_tokens"] <= 700
    text = projectnotes.load(proj / "PROJECT_NOTES.md")
    assert "volume slider" in text and "Run with python -m player." in text and a.iters_since_notes == 0
