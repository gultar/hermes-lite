"""memory: Hermes-style persistent notes (MEMORY.md) and user profile (USER.md)."""

from __future__ import annotations

from pathlib import Path

from .base import Tool, ToolContext, error, ok, register, short

DELIM = "\n§\n"
LIMITS = {"memory": 2200, "user": 1375}  # Hermes' default character budgets
FILES = {"memory": "MEMORY.md", "user": "USER.md"}
HEADERS = {"memory": "MEMORY (your personal notes)", "user": "USER PROFILE (who the user is)"}


class MemoryStore:
    def __init__(self, home: Path):
        self.dir = home / "memories"
        self.dir.mkdir(parents=True, exist_ok=True)

    def entries(self, target: str) -> list[str]:
        path = self.dir / FILES[target]
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8-sig")
        return [e.strip() for e in raw.split(DELIM) if e.strip()]

    def save(self, target: str, entries: list[str]) -> None:
        (self.dir / FILES[target]).write_text(DELIM.join(entries) + ("\n" if entries else ""), encoding="utf-8")

    def usage(self, target: str, entries: list[str] | None = None) -> str:
        n = len(DELIM.join(self.entries(target) if entries is None else entries))
        return f"{n}/{LIMITS[target]} chars, {round(100 * n / LIMITS[target])}%"

    def block(self, target: str) -> str:
        entries = self.entries(target)
        if not entries:
            return ""
        sep = "═" * 46
        return f"{sep}\n{HEADERS[target]} [{self.usage(target, entries)}]\n{sep}\n{DELIM.join(entries)}"


def _apply(entries: list[str], op: dict) -> str | None:
    action = op.get("action")
    content = (op.get("content") or op.get("new_text") or "").strip()
    old = (op.get("old_text") or "").strip()
    if action == "add":
        if not content:
            return "'add' needs content."
        if content in entries:
            return None
        entries.append(content)
        return None
    if action in ("patch", "remove"):
        if not old:
            return f"'{action}' needs old_text (text from one existing entry)."
        hits = [i for i, e in enumerate(entries) if old in e]
        if len(hits) != 1:
            return f"old_text matches {len(hits)} entries; it must match exactly one."
        i = hits[0]
        if action == "remove":
            entries.pop(i)
        else:
            if not content:
                return "'patch' needs content (the replacement for old_text)."
            entries[i] = entries[i].replace(old, content, 1)
        return None
    return f"Unknown action {action!r}; use add, patch or remove."


def memory(args: dict, ctx: ToolContext) -> str:
    store: MemoryStore = ctx.state["memory_store"]
    target = args.get("target", "memory")
    if target not in LIMITS:
        return error("target must be 'memory' or 'user'.")
    ops = args.get("operations") or ([{k: args.get(k) for k in ("action", "content", "old_text", "new_text")}]
                                     if args.get("action") else [])
    if not ops:
        return error("Give an action (add/patch/remove) or an 'operations' array.")
    entries = store.entries(target)
    for op in ops:
        if msg := _apply(entries, op):
            return error(f"Nothing was applied: {msg}", current_entries=store.entries(target))
    size = len(DELIM.join(entries))
    if size > LIMITS[target]:
        return error(f"Over the {LIMITS[target]}-char budget ({size} chars); nothing was applied. Reissue as ONE "
                     "batch that removes or shortens stale entries and adds the new one together.",
                     current_entries=store.entries(target))
    store.save(target, entries)
    return ok(success=True, done=True, target=target, usage=store.usage(target, entries),
              entry_count=len(entries), note="Write saved. This update is complete — do not repeat it.")


register(Tool(
    name="memory", emoji="🧠",
    schema={"name": "memory", "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is injected into every "
        "future session, so keep entries compact and high-signal.\n\nHOW: make ALL your changes to one target "
        "in ONE call via an 'operations' array (each item: {action, content?, old_text?}). The batch applies "
        "atomically and the char limit is checked only on the FINAL result. One batch call finishes the update, "
        "so don't repeat it. Use the bare action/content/old_text fields only for a single lone change.\n\n"
        "WHEN: only for facts that apply to EVERY session regardless of task: who the user is, stable "
        "environment facts, standing conventions with no task home. Procedures and pitfalls learned while doing "
        "a task belong in a skill via skill_manage.\n\nTARGETS: 'user' = who the user is (name, role, "
        "preferences, style). 'memory' = your notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, completed-work "
        "logs, temporary TODO state."), "parameters": {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "patch", "remove"], "description": "Single-op shape. Omit when using 'operations'."},
            "target": {"type": "string", "enum": ["memory", "user"], "description": "'memory' for your notes, 'user' for the user profile."},
            "content": {"type": "string", "description": "For 'add', the new entry. For 'patch', the text that replaces old_text."},
            "old_text": {"type": "string", "description": "REQUIRED for 'patch' and 'remove': text from one entry."},
            "operations": {"type": "array", "description": "Batch: list of {action, content?, old_text?} applied atomically.",
                           "items": {"type": "object", "properties": {
                               "action": {"type": "string", "enum": ["add", "patch", "remove"]},
                               "content": {"type": "string"}, "old_text": {"type": "string"}},
                               "required": ["action"]}},
        }, "required": ["target"]}},
    handler=memory,
    label=lambda a: f"{a.get('target', 'memory')}: " + (f"{len(a['operations'])} ops" if a.get("operations")
                                                         else f"{a.get('action', '')} {short(a.get('content') or a.get('old_text') or '', 60)}"),
))
