"""notes: the model's own notebook for a project — short notes it writes the moment it learns
something it will need later (a decision, where something lives, a command that works, a pitfall,
what's next). Kept in <project root>/.hermes/notes.md (outside a project: ~/.hermes-lite/notes/).
They are shown in the system prompt at the start of every session and after every compaction, so
they survive both; while a session runs, the tool's results show them.

Also here: the todo list of a project is kept in .hermes/todos.json, so unfinished items come back
in the next session (Hermes Lite saves it whenever todo_list changes it)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .base import Tool, ToolContext, error, ok, register

MAX_NOTES = 60
MAX_CHARS = 300
LINE = re.compile(r"^- \[(\d+)\] (.*?)(?:\s+_\((\d{4}-\d\d-\d\d)\)_)?\s*$")


def project_dir(cwd: str) -> Path | None:
    from ..prompts import workspace_root
    root = workspace_root(Path(cwd))
    return root / ".hermes" if root else None


def notes_file(ctx_or_cwd, home: Path | None = None) -> Path:
    cwd = ctx_or_cwd if isinstance(ctx_or_cwd, str) else ctx_or_cwd.cwd
    home = home if home is not None else getattr(ctx_or_cwd, "home", None)
    d = project_dir(cwd)
    if d is not None:
        return d / "notes.md"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(Path(cwd).resolve())).strip("-")[-80:] or "root"
    return Path(home or Path.home() / ".hermes-lite") / "notes" / f"{slug}.md"


def read(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = LINE.match(line)
        if m:
            out.append({"id": int(m.group(1)), "text": m.group(2).strip(), "date": m.group(3) or ""})
    return out


def write(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"- [{n['id']}] {n['text']}" + (f"  _({n['date']})_" if n.get("date") else "") for n in items)
    path.write_text("<!-- Working notes the model keeps with the notes tool; shown to it at the start of every "
                    "session. Edit or delete lines freely. -->\n" + body + ("\n" if body else ""), encoding="utf-8")


def render(items: list[dict]) -> str:
    return "\n".join(f"[{n['id']}] {n['text']}" for n in items)


def notes(args: dict, ctx: ToolContext) -> str:
    path = notes_file(ctx)
    items = read(path)
    action = str(args.get("action") or ("add" if args.get("text") else "list")).lower()
    text = " ".join(str(args.get("text") or "").split())
    nid = args.get("id")
    try:
        nid = int(nid) if nid is not None and str(nid).strip() != "" else None
    except ValueError:
        return error(f"'id' must be a note number like 3, not {nid!r}.")
    if action == "list":
        return ok(file=str(path), count=len(items), notes=render(items) or "(none yet)")
    if action in ("add", "update"):
        if not text:
            return error(f"notes {action}: give the note in 'text'.")
        if len(text) > MAX_CHARS:
            return error(f"Too long ({len(text)} characters): a note is one or two lines, at most {MAX_CHARS} "
                         "characters. Keep the essential fact; details belong in the code or a file.")
    today = time.strftime("%Y-%m-%d")
    if action == "add":
        if len(items) >= MAX_NOTES:
            return error(f"The notebook is full ({MAX_NOTES} notes). Remove notes that no longer hold, or merge "
                         "related ones with update, then add this one.", notes=render(items))
        same = next((n for n in items if n["text"].lower() == text.lower()), None)
        if same:
            return ok(file=str(path), note="Already noted.", notes=render(items))
        new = {"id": max((n["id"] for n in items), default=0) + 1, "text": text, "date": today}
        items.append(new)
        write(path, items)
        return ok(file=str(path), added=new["id"], count=len(items), notes=render(items))
    target = next((n for n in items if n["id"] == nid), None)
    if target is None:
        return error(f"No note [{nid}]. Current notes:\n" + (render(items) or "(none)"))
    if action == "update":
        target.update(text=text, date=today)
    elif action == "remove":
        items.remove(target)
    else:
        return error("action must be add, update, remove or list.")
    write(path, items)
    return ok(file=str(path), count=len(items), notes=render(items) or "(none left)")


# ── the todo list of a project, across sessions ───────────────────────────
def todos_file(cwd: str) -> Path | None:
    d = project_dir(cwd)
    return d / "todos.json" if d else None


def save_todos(cwd: str, todos: list[dict]) -> None:
    path = todos_file(cwd)
    if path is None:
        return
    try:
        if todos:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(todos, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def unfinished_todos(cwd: str) -> list[dict]:
    path = todos_file(cwd)
    try:
        items = json.loads(path.read_text(encoding="utf-8")) if path else []
    except (OSError, ValueError):
        return []
    if not isinstance(items, list) or not any(isinstance(t, dict) and t.get("status") in ("pending", "in_progress")
                                              for t in items):
        return []
    return [t for t in items if isinstance(t, dict) and t.get("id")]


register(Tool(
    name="notes", emoji="🗒",
    schema={"name": "notes", "description": "(from prompts/tool_notes.md)", "parameters": {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "update", "remove", "list"],
                       "description": "add (default when text is given), update, remove, list."},
            "text": {"type": "string", "description": f"The note: one or two lines, at most {MAX_CHARS} characters."},
            "id": {"type": "integer", "description": "The note's number, for update and remove."},
        }}},
    handler=notes, label=lambda a: (a.get("action") or "add") + (f" [{a['id']}]" if a.get("id") else "")
    + (": " + str(a.get("text"))[:70] if a.get("text") else ""),
))
