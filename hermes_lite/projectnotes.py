"""Project notes: Hermes Lite's memory of one project, in <project root>/PROJECT_NOTES.md.

- Read at the start of every session (and again after a compaction) into the system prompt, so
  the model starts from what the project is, what was done and what comes next.
- Written by the model itself: after a stretch of work (agent.project_notes_after_tool_calls,
  right after the skill review), when a session ends (/new, quitting) and with /notes update. The
  request goes at the end of the conversation llama-server already has cached, so it only costs
  the writing.
- Only inside a project (a Git repository or a folder with a project file). You can edit the file
  yourself: the next update starts from your version. Earlier versions: .hermes/notes-history/.
- The first version is written early (after the first reply that used a few tools), so a project
  gets its description on day one; later updates follow every stretch of work."""

from __future__ import annotations

import re
import time
from pathlib import Path

LOAD_CAP = 9000      # characters read into the system prompt (~2.8k tokens)
HISTORY_KEPT = 10
HEADER = "<!-- Hermes Lite project notes · updated {stamp} · edit freely: Hermes Lite reads this file at the start of each session and updates it -->\n"


def notes_path(cfg, cwd: str | Path) -> Path | None:
    if not cfg.get_path("agent.project_notes", True):
        return None
    from .prompts import workspace_root
    root = workspace_root(Path(cwd))
    if root is None:
        return None
    path = root / str(cfg.get_path("agent.project_notes_file", "PROJECT_NOTES.md"))
    old = root / ".hermes" / "project.md"  # where builds from 2026-10-29 kept them
    if not path.exists() and old.is_file() and path != old:
        try:
            old.replace(path)
        except OSError:
            return old
    return path


def load(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S).strip()
    return text or None


def for_prompt(path: Path | None) -> str | None:
    text = load(path)
    if not text:
        return None
    if len(text) > LOAD_CAP:
        text = text[: int(LOAD_CAP * 0.75)] + f"\n\n[… shortened here; the whole file is {path} …]\n\n" + \
            text[-int(LOAD_CAP * 0.2):]
    return text


def clean(reply: str) -> str | None:
    """The model's reply as a notes file, or None when it is not one."""
    text = (reply or "").strip()
    fence = re.match(r"\A```[a-zA-Z]*\n(.*)\n```\s*\Z", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if text.count("\n## ") < 2 or len(text) < 200:
        return None
    return text


def save(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        hist = path.parent / ".hermes" / "notes-history"
        hist.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (hist / f"project-{stamp}.md").write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        for old in sorted(hist.glob("project-*.md"))[:-HISTORY_KEPT]:
            old.unlink(missing_ok=True)
    path.write_text(HEADER.format(stamp=time.strftime("%Y-%m-%d %H:%M")) + text.strip() + "\n", encoding="utf-8")
    return path


# ── updates that send only what changed ──────────────────────────────────
DONE = re.compile(r"^done\b", re.I)


def _sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(everything before the first '## ', [(heading line, body)])."""
    parts = re.split(r"(?m)^(?=## )", text.strip())
    head = parts[0] if parts and not parts[0].startswith("## ") else ""
    out = []
    for part in parts:
        if part.startswith("## "):
            first, _, body = part.partition("\n")
            out.append((first.strip(), body.strip("\n")))
    return head.strip(), out


def done_bullets(text: str) -> int:
    for heading, body in _sections(text)[1]:
        if DONE.match(heading[3:].strip()):
            return sum(1 for line in body.splitlines() if line.lstrip().startswith(("- ", "* ")))
    return 0


def merge(current: str, delta: str) -> str | None:
    """Apply a 'changes only' reply: new 'Done so far' bullets are appended; any other section that
    appears replaces the old one (a new section is added at the end). None: nothing to change."""
    reply = (delta or "").strip()
    fence = re.match(r"\A```[a-zA-Z]*\n(.*)\n```\s*\Z", reply, re.S)
    if fence:
        reply = fence.group(1).strip()
    if not reply or reply.upper().startswith("NO CHANGES") or "## " not in reply:
        return None
    head, sections = _sections(current)
    _, changes = _sections(reply)
    by_name = {h[3:].strip().lower(): i for i, (h, _) in enumerate(sections)}
    changed = False
    for heading, body in changes:
        name = heading[3:].strip().lower()
        if not body.strip():
            continue
        i = by_name.get(name)
        if DONE.match(name):
            if i is None:
                sections.append((heading, body))
                by_name[name] = len(sections) - 1
                changed = True
                continue
            old = sections[i][1]
            have = {line.strip() for line in old.splitlines()}
            new = [line for line in body.splitlines() if line.strip() and line.strip() not in have]
            if new:
                sections[i] = (sections[i][0], (old.rstrip() + "\n" + "\n".join(new)).strip("\n"))
                changed = True
        elif i is None:
            sections.append((heading, body))
            by_name[name] = len(sections) - 1
            changed = True
        elif sections[i][1].strip() != body.strip():
            sections[i] = (sections[i][0], body)
            changed = True
    if not changed:
        return None
    return "\n\n".join(([head] if head else []) + [f"{h}\n{b}" for h, b in sections]).strip()
