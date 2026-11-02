"""skills_list, skill_view, skill_manage — Hermes/agentskills-style SKILL.md folders."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from . import fuzzy
from .base import Tool, ToolContext, error, ok, register, short

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")
    return {}, text


def roots(ctx: ToolContext) -> list[Path]:
    own = ctx.home / "skills"
    extra = [Path(p).expanduser() for p in (ctx.cfg.get_path("skills.extra_dirs") or [])]
    return [own] + [p for p in extra if p.is_dir()]


def catalog(ctx: ToolContext) -> dict[str, dict]:
    """name -> {path, category, description, writable}; the user's own folder wins on duplicates."""
    found: dict[str, dict] = {}
    for i, root in enumerate(roots(ctx)):
        if not root.is_dir():
            continue
        for md in sorted(root.rglob("SKILL.md")):
            if any(part.startswith(".") for part in md.relative_to(root).parts):
                continue
            meta, body = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            name = str(meta.get("name") or md.parent.name)
            if name in found:
                continue
            rel = md.parent.relative_to(root).parts
            category = "/".join(rel[:-1]) or "general"
            desc = str(meta.get("description") or next((l for l in body.splitlines() if l.strip()), ""))
            found[name] = {"path": md, "category": category, "description": short(desc, 180), "writable": i == 0}
    return found


def skills_index(ctx: ToolContext) -> str:
    cat = catalog(ctx)
    if not cat:
        return ""
    by_cat: dict[str, list[str]] = {}
    for name, info in sorted(cat.items()):
        by_cat.setdefault(info["category"], []).append(f"    - {name}: {info['description']}")
    return "\n".join(f"  {c}:\n" + "\n".join(rows) for c, rows in sorted(by_cat.items()))


def skills_list(args: dict, ctx: ToolContext) -> str:
    cat = catalog(ctx)
    want = args.get("category")
    rows = [{"name": n, "category": i["category"], "description": i["description"]}
            for n, i in sorted(cat.items()) if not want or i["category"].startswith(want)]
    return ok(skills=rows, count=len(rows))


def skill_view(args: dict, ctx: ToolContext) -> str:
    name = args.get("name", "")
    info = catalog(ctx).get(name)
    if not info:
        return error(f"No skill named {name!r}.", available=sorted(catalog(ctx))[:50])
    folder: Path = info["path"].parent
    rel = args.get("file_path")
    if rel:
        target = (folder / rel).resolve()
        if folder.resolve() not in target.parents or not target.is_file():
            return error(f"No file {rel!r} in skill {name!r}.")
        return ok(name=name, file_path=rel, content=target.read_text(encoding="utf-8", errors="replace"))
    linked = {}
    for sub in ("references", "templates", "scripts", "assets"):
        d = folder / sub
        if d.is_dir():
            linked[sub] = [str(p.relative_to(folder)) for p in sorted(d.rglob("*")) if p.is_file()]
    return ok(name=name, path=str(info["path"]), content=info["path"].read_text(encoding="utf-8", errors="replace"),
              linked_files=linked)


def skill_manage(args: dict, ctx: ToolContext) -> str:
    ops = args.get("operations") or [args]
    results = []
    for op in ops:
        res = _one(op, ctx)
        if res.startswith("ERR:"):
            return error(res[4:], completed=results)
        results.append(res)
    return ok(success=True, results=results)


def _one(op: dict, ctx: ToolContext) -> str:
    action, name = op.get("action"), str(op.get("name") or "")
    if not NAME_RE.match(name):
        return "ERR:'name' must be lowercase letters, digits, hyphens/underscores (max 64 chars)."
    info = catalog(ctx).get(name)
    if action == "create":
        if info:
            return f"ERR:Skill {name!r} already exists; use action='patch'."
        content = op.get("content")
        if not content:
            return "ERR:'create' needs content: the full SKILL.md (YAML frontmatter with name + description, then markdown)."
        if not content.lstrip().startswith("---"):
            content = f"---\nname: {name}\ndescription: {short(content, 120)}\n---\n\n{content}"
        folder = ctx.home / "skills" / (op.get("category") or "") / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(content, encoding="utf-8")
        return f"created {name}"
    if not info:
        return f"ERR:No skill named {name!r}."
    if not info["writable"]:
        return f"ERR:Skill {name!r} lives in a read-only extra_dirs folder; create your own copy instead."
    folder: Path = info["path"].parent
    if action == "patch":
        target = folder / (op.get("file_path") or "SKILL.md")
        if op.get("content") and "old_string" not in op:
            target.write_text(op["content"], encoding="utf-8")
            return f"rewrote {target.name} in {name}"
        if not target.is_file():
            return f"ERR:No file {target.name} in {name}."
        try:
            new, _, n = fuzzy.apply(target.read_text(encoding="utf-8"), str(op.get("old_string", "")),
                                    str(op.get("new_string", "")), bool(op.get("replace_all")))
        except ValueError as exc:
            return f"ERR:{exc}"
        target.write_text(new, encoding="utf-8")
        return f"patched {target.name} in {name} ({n} replacement{'s' if n > 1 else ''})"
    if action == "write_file":
        rel = op.get("file_path") or ""
        if not rel or rel == "SKILL.md" or rel.startswith(("/", "..")):
            return "ERR:write_file needs a relative file_path such as references/notes.md."
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(str(op.get("file_content", "")), encoding="utf-8")
        return f"wrote {rel} in {name}"
    if action == "remove_file":
        target = folder / str(op.get("file_path") or "")
        if target.is_file() and target.name != "SKILL.md":
            target.unlink()
            return f"removed {op.get('file_path')} from {name}"
        return "ERR:No such supporting file."
    if action == "delete":
        shutil.rmtree(folder)
        return f"deleted {name}"
    return f"ERR:Unknown action {action!r}."


register(Tool(
    name="skills_list", emoji="📚", read_only=True,
    schema={"name": "skills_list", "description": "List available skills (name + description). Use skill_view(name) to load full content.",
            "parameters": {"type": "object", "properties": {
                "category": {"type": "string", "description": "Optional category filter to narrow results"}}}},
    handler=skills_list, label=lambda a: a.get("category", ""),
))

register(Tool(
    name="skill_view", emoji="📚", read_only=True,
    schema={"name": "skill_view", "description": (
        "Skills allow for loading information about specific tasks and workflows, as well as scripts and "
        "templates. Load a skill's full content or access its linked files (references, templates, scripts). "
        "First call returns SKILL.md content plus a 'linked_files' dict. To access those, call again with "
        "file_path."), "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The skill name."},
            "file_path": {"type": "string", "description": "OPTIONAL: path to a linked file within the skill, e.g. 'references/api.md'."},
        }, "required": ["name"]}},
    handler=skill_view, label=lambda a: a.get("name", "") + (f" / {a['file_path']}" if a.get("file_path") else ""),
))

register(Tool(
    name="skill_manage", emoji="📝",
    schema={"name": "skill_manage", "description": (
        "Create and maintain skills: reusable procedures, pitfalls and the user's conventions for a kind of task. "
        "Actions: create (name, content = full SKILL.md with YAML frontmatter name/description + markdown "
        "body, optional category), patch (name, old_string, new_string; optional file_path for a supporting "
        "file), write_file (name, file_path, file_content), remove_file (name, file_path), delete (name). "
        "Pass several changes as 'operations'."), "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "patch", "write_file", "remove_file", "delete"]},
            "name": {"type": "string", "description": "Skill name: lowercase, hyphens/underscores, max 64 chars."},
            "content": {"type": "string", "description": "create: full SKILL.md text."},
            "category": {"type": "string", "description": "create: optional category subfolder (e.g. 'devops')."},
            "old_string": {"type": "string", "description": "patch: exact text to replace."},
            "new_string": {"type": "string", "description": "patch: replacement text."},
            "file_path": {"type": "string", "description": "Supporting file path inside the skill (e.g. references/notes.md)."},
            "file_content": {"type": "string", "description": "write_file: full text of the supporting file."},
            "operations": {"type": "array", "description": "Several ops, each with the same fields as above.", "items": {"type": "object"}},
        }}},
    handler=skill_manage,
    label=lambda a: f"{a.get('action', '')} {a.get('name', '')}".strip() or f"{len(a.get('operations') or [])} ops",
))
