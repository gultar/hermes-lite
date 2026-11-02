"""todo_list: the task list shown in the sidebar."""

from __future__ import annotations

from .base import Tool, ToolContext, error, ok, register

STATUSES = ("pending", "in_progress", "completed", "cancelled")


def todos(ctx: ToolContext) -> list[dict]:
    return ctx.state.setdefault("todos", [])


def _summary(items: list[dict]) -> str:
    counts = {s: sum(1 for t in items if t["status"] == s) for s in STATUSES}
    return ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "empty"


def todo_list(args: dict, ctx: ToolContext) -> str:
    current = todos(ctx)
    incoming = args.get("todos")
    if incoming is None:
        return ok(todos=current, summary=_summary(current))
    if not isinstance(incoming, list):
        return error("'todos' must be an array of {id, content, status} objects.")
    cleaned = []
    for item in incoming:
        if not isinstance(item, dict) or not item.get("id"):
            return error("Every todo needs an 'id'.", bad_item=item)
        status = item.get("status", "pending")
        if status not in STATUSES:
            return error(f"Invalid status {status!r}; use one of {', '.join(STATUSES)}.")
        cleaned.append({"id": str(item["id"]), "content": str(item.get("content", "")), "status": status,
                        **({"parent": str(item["parent"])} if item.get("parent") else {})})
    if args.get("merge"):
        by_id = {t["id"]: t for t in current}
        for item in cleaned:
            if item["id"] in by_id:
                by_id[item["id"]].update({k: v for k, v in item.items() if v != "" or k != "content"})
            else:
                current.append(item)
    else:
        current[:] = cleaned
    note = None
    if sum(1 for t in current if t["status"] == "in_progress") > 1:
        note = "More than one item is in_progress; keep only ONE in_progress at a time."
    from .notes import save_todos
    save_todos(ctx.cwd, current)  # the project's list survives the session
    return ok(todos=current, summary=_summary(current), **({"note": note} if note else {}))


register(Tool(
    name="todo_list", emoji="📋",
    schema={"name": "todo_list", "description": (
        "Track a task list for multi-step work (3+ steps). Use for complex tasks with 3+ steps or when the user "
        "provides multiple tasks. For 'all N items' tasks, enumerate every instance as its own checklist item so "
        "none are silently dropped. Call with no parameters to read the current list.\n"
        "List order is priority. Only ONE item in_progress at a time. Break large phases into subtasks via parent. "
        "Mark an item completed only after the work is verified done, never based on intent. If something fails, "
        "cancel it and add a revised item. Always returns the full current list."), "parameters": {
        "type": "object", "properties": {
            "todos": {"type": "array", "description": "Task items to write.", "items": {
                "type": "object", "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string", "description": "Task description"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "parent": {"type": "string", "description": "Optional id of another item, making this a nested subtask. Omit for top-level."},
                }, "required": ["id", "content", "status"]}},
            "merge": {"type": "boolean", "default": False, "description": "true: update existing items by id, add new ones. false (default): replace the entire list with a fresh plan."},
        }}},
    handler=todo_list,
    label=lambda a: "read" if a.get("todos") is None else f"{len(a.get('todos') or [])} items{' (merge)' if a.get('merge') else ''}",
))
