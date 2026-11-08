"""A catalogue of skills to browse (/skills browse): Hermes Agent's skills, or any folder of SKILL.md
skills, listed with what each does and what might not work here. Adding one copies it into
~/.hermes-lite/skills, so only the skills you pick reach the model's prompt."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from .tools.skills import parse_frontmatter

DEFAULT_SOURCES = ["~/.hermes/skills", "~/.hermes/hermes-agent/skills", "~/.hermes/hermes-agent/optional-skills"]
MARK = ".from-catalog"   # in a copied skill's folder: where it came from (so /skills browse can remove it)

# Hermes Agent tools that Hermes Lite does not have, as a skill's text names them
_HERMES_ONLY = re.compile(r"\b(delegate_task|execute_code|browser_[a-z_]+|image_generate|text_to_speech|cronjob|"
                          r"send_message|kanban_[a-z_]+|session_search|mixture_of_agents)\b")
_LITE_TOOLSETS = {"terminal", "file", "files", "web", "vision", "skills", "memory", "todo", "clarify"}
_PLATFORM = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"


def sources(cfg) -> list[Path]:
    dirs = cfg.get_path("skills.catalog")
    dirs = DEFAULT_SOURCES if dirs is None else dirs
    return [p for p in (Path(str(d)).expanduser() for d in dirs) if p.is_dir()]


def _list(value) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]


def hints(meta: dict, text: str) -> list[str]:
    """What may not work in Hermes Lite, read from the skill itself (not a verdict: a pointer)."""
    out = []
    plats = [{"mac": "macos", "darwin": "macos", "osx": "macos", "win": "windows"}.get(p.lower(), p.lower())
             for p in _list(meta.get("platforms"))]
    if plats and _PLATFORM not in plats:
        out.append(f"{', '.join(plats)} only")
    m = meta.get("metadata")
    h = (m.get("hermes") if isinstance(m, dict) else None) or {}
    h = h if isinstance(h, dict) else {}
    sessions = [s.lower() for s in _list(h.get("session_platforms"))]
    if sessions and "cli" not in sessions:
        out.append(f"only for Hermes Agent's {', '.join(sessions)} sessions")
    need = [t for t in _list(h.get("requires_toolsets")) if t not in _LITE_TOOLSETS]
    if need:
        out.append("needs " + ", ".join(need))
    tools = []
    for t in _HERMES_ONLY.findall(text):
        t = "browser tools" if t.startswith("browser_") else "kanban" if t.startswith("kanban_") else t
        if t not in tools:
            tools.append(t)
    if tools:
        out.append("uses " + ", ".join(tools) + " (not in Hermes Lite)")
    return out


def installed(home: Path) -> dict[str, Path]:
    """name -> folder of the skills already in ~/.hermes-lite/skills."""
    out = {}
    root = home / "skills"
    for md in sorted(root.rglob("SKILL.md")) if root.is_dir() else []:
        meta, _ = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        out.setdefault(str(meta.get("name") or md.parent.name), md.parent)
    return out


def entries(cfg, home: Path) -> list[dict]:
    """Every skill in the catalogue folders (the first folder wins on duplicate names)."""
    have = installed(home)
    seen: dict[str, dict] = {}
    for src in sources(cfg):
        for md in sorted(src.rglob("SKILL.md")):
            rel = md.parent.relative_to(src).parts
            if any(p.startswith(".") for p in rel):
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_frontmatter(text)
            name = str(meta.get("name") or md.parent.name)
            if name in seen:
                continue
            desc = str(meta.get("description") or next((l for l in body.splitlines() if l.strip()), ""))
            seen[name] = {"name": name, "category": "/".join(rel[:-1]) or "general", "description": desc.strip(),
                          "dir": md.parent, "source": str(src), "body": body, "hints": hints(meta, text),
                          "added": name in have,
                          "removable": name in have and (have[name] / MARK).is_file()}
    return sorted(seen.values(), key=lambda e: (e["category"], e["name"]))


def matches(entry: dict, query: str) -> bool:
    hay = f"{entry['name']} {entry['category']} {entry['description']}".lower()
    return all(w in hay for w in query.lower().split())


def add(entry: dict, home: Path) -> Path:
    dest = home / "skills" / entry["category"] / entry["name"]
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    shutil.copytree(entry["dir"], dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    (dest / MARK).write_text(str(entry["dir"]) + "\n", encoding="utf-8")
    return dest


def remove(name: str, home: Path) -> Path:
    """Only a skill added from the catalogue (your own skills are never removed here)."""
    folder = installed(home).get(name)
    if not folder or not (folder / MARK).is_file():
        raise PermissionError(f"{name} was not added from the catalogue")
    shutil.rmtree(folder)
    return folder
