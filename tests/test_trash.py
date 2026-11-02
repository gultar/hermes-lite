"""Files in the desktop's Trash count as deleted."""
import json
import threading
import time

from hermes_lite.agent import Agent
from hermes_lite.config import load_config
from hermes_lite.llm import ToolCall
from hermes_lite.tools import REGISTRY
from hermes_lite.tools.base import in_trash


class UI:
    def event(self, *a, **k):
        pass


def _agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg["agent"]["approvals"] = "off"
    return Agent(cfg, UI(), cwd=str(proj)), proj


def _call(a, name, **args):
    c = ToolCall(id="t1", name=name, arguments=json.dumps(args))
    c.parsed = args
    return json.loads(a._run_tool_sync(c))


def test_in_trash(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert in_trash(tmp_path / "share" / "Trash" / "files" / "movi (3)" / "main.py")
    assert in_trash("/run/media/me/Stockage/.Trash-1000/files/x.py")
    assert not in_trash(tmp_path / "Documents" / "movi" / "main.py")
    assert not in_trash(tmp_path / "share" / "TrashCanApp" / "a.py")


def test_tools_refuse_the_trash(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    old = tmp_path / "share" / "Trash" / "files" / "movi (3)"
    old.mkdir(parents=True)
    (old / "main.py").write_text("print(1)\n")
    (proj / "main.py").write_text("print(2)\n")
    for name, args in [("read_file", {"path": str(old / "main.py")}), ("search_files", {"pattern": "print", "path": str(old)}),
                       ("write_file", {"path": str(old / "main.py"), "content": "x"}),
                       ("terminal", {"command": "cat ~/.local/share/Trash/files/movi/main.py"}),
                       ("terminal", {"command": "ls", "workdir": str(old)})]:
        r = _call(a, name, **args)
        assert r.get("success") is False and "Trash" in r["error"] and str(proj) in r["error"], name
    assert "print(2)" in _call(a, "read_file", path="main.py")["content"]
    assert (old / "main.py").read_text() == "print(1)\n"


def test_working_folder_moved_to_the_trash_falls_back(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    work = tmp_path / "Documents" / "movi"
    work.mkdir(parents=True)
    _call(a, "terminal", command=f"cd '{work}'")
    assert a.ctx.cwd == str(work)
    trash = tmp_path / "share" / "Trash" / "files"
    trash.mkdir(parents=True)

    def move():  # the user sends the folder to the Trash while a command runs in it
        time.sleep(0.4)
        work.rename(trash / "movi (3)")
    threading.Thread(target=move).start()
    r = _call(a, "terminal", command="sleep 1; cd .")
    assert a.ctx.cwd == str(proj), r
    assert "Trash" in r.get("note", "") or "no longer exists" in r.get("note", "")


def test_a_moved_project_is_not_followed(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    work = tmp_path / "Documents" / "movi"
    work.mkdir(parents=True)
    _call(a, "terminal", command=f"cd '{work}'")

    def move():  # the user moves the old project away and puts a new one at the same path
        time.sleep(0.4)
        work.rename(tmp_path / "Documents" / "movi-old")
        work.mkdir()
        (work / "new.txt").write_text("new project")
    threading.Thread(target=move).start()
    _call(a, "terminal", command="sleep 1")
    assert a.ctx.cwd == str(work)                       # the path you work in, not where the old folder went
    assert _call(a, "terminal", command="cat new.txt")["output"] == "new project"


def test_resume_runs_in_the_folder_you_launched_from(tmp_path, monkeypatch):
    a, proj = _agent(tmp_path, monkeypatch)
    a.messages.append({"role": "user", "content": "hi"})
    a.ctx.cwd = str(tmp_path / "share" / "Trash" / "files" / "movi (3)")   # where the old session ended up
    data = {"id": a.session_id, "messages": a.messages, "cwd": a.ctx.cwd}
    b = Agent(a.cfg, UI(), cwd=str(proj))
    b.load(data)
    assert b.ctx.cwd == str(proj) and f"Current working directory: {proj}" in b.messages[0]["content"]
