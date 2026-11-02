"""Prompts as Markdown files you can edit.

Every text the model reads — system prompt blocks, tool descriptions, the notes Hermes Lite adds
mid-conversation — lives in hermes_lite/prompts/<name>.md. To change one, copy it to
~/.hermes-lite/prompts/<name>.md and edit the copy: yours wins, survives updates, and deleting it
brings the default back. `/prompts` lists them all.

Placeholders are written {{name}} (only that exact form is replaced, so code and JSON in a prompt
are safe). A file's first lines may be an HTML comment (<!-- … -->): it is documentation and is
not sent to the model.

Two built-in styles (prompts.style): "compact" (default) uses the shorter files in prompts/compact/
where one exists (an empty one leaves that block out) and the shorter parameter descriptions in
prompts/compact/params.yaml; "hermes" uses the original Hermes wording everywhere.

Safety: an override that is empty, unreadable, or lacks a placeholder or heading Hermes Lite needs
is ignored with a warning, and the default is used; an unknown placeholder is left as written,
with a warning. A missing default file is a broken install: the error says so."""

from __future__ import annotations

import re
from pathlib import Path

from .config import home

PKG_DIR = Path(__file__).resolve().parent / "prompts"
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
HEADER = re.compile(r"\A\s*<!--.*?-->[ \t]*\n?", re.S)

# name: (what it is, placeholders it may use, text that must stay in it)
SPECS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "identity": ("System prompt: who the model is. ~/.hermes-lite/SOUL.md, if present, replaces it.", (), ()),
    "task_completion": ("System prompt: finishing the job before stopping.", (), ()),
    "parallel_tool_calls": ("System prompt: batching independent tool calls.", (), ()),
    "memory_guidance": ("System prompt: what to save with the memory tool (only when memory is on).", (), ()),
    "skills_guidance": ("System prompt: saving workflows as skills (only when skill_manage is on).", (), ()),
    "steering": ("System prompt: messages the user types while the model works.",
                 ("steer_open", "steer_close"), ("{{steer_open}}", "{{steer_close}}")),
    "tool_use": ("System prompt: use tools instead of guessing; what always needs a tool.", (), ()),
    "execution": ("System prompt: execution discipline (verify, don't stop early).", (), ()),
    "coding": ("System prompt: the coding brief (only inside a code project).", (), ()),
    "tui_platform": ("System prompt: how replies are displayed in the terminal UI.", (), ()),
    "skills_block": ("System prompt: the list of saved skills (only when there are some).",
                     ("skills",), ("{{skills}}",)),
    "loop_guard_line": ("System prompt: stop thinking in circles (only when loop_guard is on).", (), ()),
    "empty_reply": ("Note after an empty reply (no answer, no tool call).", (), ()),
    "thought_only": ("Note after a reply that only thought, without answering or calling a tool.", (), ()),
    "repeated_call": ("Added to a tool result when the same call was made several times in a row.",
                      ("n",), ()),
    "reading_streak": ("Added to a tool result after many file reads in a row without acting.", ("n",), ()),
    "loop_retry": ("Sent when a reply was stopped because the thinking went in circles.", ("excerpt",), ()),
    "iteration_budget": ("Sent when the tool-call budget for a message is used up.", (), ()),
    "thinking_budget": ("Inserted by llama-server when the thinking budget runs out (loop guard).", (), ()),
    "skill_review": ("The background skill review request.", ("skills",), ("{{skills}}",)),
    "plan_mode": ("Put before your first message in /plan mode.", (), ("## Goal", "## Steps")),
    "plan_followup": ("Put before later messages while in /plan mode.", (), ()),
    "plan_nudge": ("Added to tool results after many investigation steps in /plan mode.", ("n",), ()),
    "plan_write_now": ("Sent when the plan must be written now (/build without a plan, or the step limit).",
                       (), ("## Goal", "## Steps")),
    "build": ("The /build instruction; {{plan}} is the plan file, {{edited}} notes your edits.",
              ("plan", "edited"), ("{{plan}}",)),
    "compaction_handoff": ("Heads the summary that replaces compacted history.", (), ()),
    "notes_block": ("System prompt: your working notes and unfinished todo items for this project.",
                    ("notes", "path", "todos"), ()),
    "project_notes_block": ("System prompt: the project notes (only inside a project that has them).",
                            ("notes", "path"), ("{{notes}}",)),
    "project_notes_update": ("The request to rewrite the project notes, sent at the end of the conversation.",
                             ("current", "facts", "today", "limit"), ("{{current}}",)),
    "project_notes_delta": ("The short 'what changed' request for the project notes.",
                            ("current", "today"), ("{{current}}",)),
    "compaction_inplace": ("Asks for the checkpoint summary at the end of the conversation (followed by the "
                           "compaction_template).", (), ()),
    "compaction_current": ("Ends the compaction handoff when the request being worked on was compacted.",
                           ("request",), ("{{request}}",)),
    "compaction_template": ("The structure the model fills in when compacting history.",
                            ("budget", "today"), ()),
}
TOOL_PREFIX = "tool_"  # tool_<name>.md: that tool's description (its parameters stay in code)

WARNINGS: list[str] = []
_cache: dict[str, tuple[str, str]] = {}


def user_dir() -> Path:
    return home() / "prompts"


def _spec(name: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if name in SPECS:
        return SPECS[name]
    if name.startswith(TOOL_PREFIX):
        return (f"Description of the {name[len(TOOL_PREFIX):]} tool.", (), ())
    return SPECS[name]


def _warn(msg: str) -> None:
    if msg not in WARNINGS:
        WARNINGS.append(msg)


def body(text: str) -> str:
    """File contents without the documentation comment, and without the editor's final newline."""
    text = HEADER.sub("", text, count=1)
    return text[:-1] if text.endswith("\n") else text


STYLES = ("compact", "hermes")
STYLE = "compact"  # prompts.style, or --hermes-prompts for one run


def default_path(name: str) -> Path:
    """The built-in file in use for *name* under the current style."""
    compact = PKG_DIR / "compact" / f"{name}.md"
    return compact if STYLE == "compact" and compact.is_file() else PKG_DIR / f"{name}.md"


def set_style(style: str) -> None:
    global STYLE
    STYLE = style if style in STYLES else "compact"
    reload()


def param_descriptions(tool: str) -> dict:
    """Shorter parameter descriptions for *tool* (compact style only)."""
    if STYLE != "compact":
        return {}
    if "__params" not in _cache:
        try:
            import yaml
            data = yaml.safe_load((PKG_DIR / "compact" / "params.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        _cache["__params"] = (data, "default")  # type: ignore[assignment]
    data = _cache["__params"][0]
    return data.get(tool) or {} if isinstance(data, dict) else {}


def _default(name: str) -> str:
    path = default_path(name)
    try:
        return body(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Hermes Lite's prompt file {path} is missing ({exc}). The install is incomplete: run "
                           "./install.sh again, or hermes-lite --rollback.") from exc


ENABLED = True  # False ignores every override (prompts.overrides: false, or --default-prompts)


def raw(name: str) -> tuple[str, str]:
    """(template, source) — source is 'default' or the override's path."""
    if name in _cache:
        return _cache[name]
    _, _, need = _spec(name)
    text, source = _default(name), "default"
    override = user_dir() / f"{name}.md"
    if ENABLED and override.is_file():
        try:
            mine = body(override.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            _warn(f"prompts/{name}.md could not be read ({exc}); using the default.")
        else:
            missing = [n for n in need if n not in mine]
            if not mine.strip():
                _warn(f"prompts/{name}.md is empty; using the default.")
            elif missing:
                _warn(f"prompts/{name}.md lacks {', '.join(missing)}, which Hermes Lite needs; using the default.")
            else:
                text, source = mine, str(override)
    _cache[name] = (text, source)
    return text, source


def get(name: str, **values) -> str:
    """The prompt with its {{placeholders}} filled in."""
    text, source = raw(name)
    allowed = set(_spec(name)[1])

    def fill(m: re.Match) -> str:
        key = m.group(1)
        if key in values and (key in allowed or not allowed):
            return str(values[key])
        if source != "default":
            _warn(f"prompts/{name}.md: unknown placeholder {{{{{key}}}}} left as written "
                  f"(available: {', '.join('{{' + a + '}}' for a in sorted(allowed)) or 'none'}).")
        return m.group(0)

    return PLACEHOLDER.sub(fill, text)


def reload() -> None:
    """Forget cached files (after you edit an override)."""
    _cache.clear()
    WARNINGS.clear()


def listing() -> list[tuple[str, str, str]]:
    """(name, 'default' | 'yours' | 'ignored', what it is) for every prompt file. 'ignored': you have
    a copy, but it was unusable (see WARNINGS) or your copies are switched off."""
    names = sorted(p.stem for p in PKG_DIR.glob("*.md"))
    rows = []
    for n in names:
        _, source = raw(n)
        state = "yours" if source != "default" else ("ignored" if (user_dir() / f"{n}.md").is_file() else "default")
        rows.append((n, state, _spec(n)[0] if (n in SPECS or n.startswith(TOOL_PREFIX)) else ""))
    return rows


def header(name: str) -> str:
    about, placeholders, need = _spec(name)
    lines = [f"Hermes Lite prompt: {name}", about]
    if placeholders:
        lines.append("Placeholders: " + ", ".join("{{" + p + "}}" for p in placeholders))
    if need:
        lines.append("Keep in your version: " + ", ".join(need) + " (Hermes Lite relies on them).")
    lines.append(f"To change it: copy this file to ~/.hermes-lite/prompts/{name}.md and edit the copy.")
    return "<!--\n" + "\n".join(lines) + "\n-->\n"


# ── your copies: create, undo, reset ────────────────────────────────────
def _history_dir() -> Path:
    d = user_dir() / ".history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_overrides() -> None:
    """Keep the last 10 versions of each file you changed (taken at launch), so an edit can be undone."""
    import time
    if not user_dir().is_dir():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S") + f".{time.time_ns() // 1000 % 1_000_000:06d}"
    for f in sorted(user_dir().glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        saved = sorted(_history_dir().glob(f"{f.stem}@*.md"))
        if saved and saved[-1].read_text(encoding="utf-8") == text:
            continue
        (_history_dir() / f"{f.stem}@{stamp}.md").write_text(text, encoding="utf-8")
        for old in saved[:-9]:
            old.unlink(missing_ok=True)


def edit(name: str) -> Path:
    """Copy the default into your prompts folder (if not there yet) and return the path to edit."""
    if not (PKG_DIR / f"{name}.md").is_file():
        raise KeyError(name)
    target = user_dir() / f"{name}.md"
    if not target.exists():
        user_dir().mkdir(parents=True, exist_ok=True)
        target.write_text(default_path(name).read_text(encoding="utf-8"), encoding="utf-8")
    return target


def reset(name: str) -> Path | None:
    """Stop using your version of *name* (kept as <name>.md.off, and in the history)."""
    target = user_dir() / f"{name}.md"
    if not target.exists():
        return None
    snapshot_overrides()
    off = target.with_suffix(".md.off")
    target.replace(off)
    reload()
    return off


def undo(name: str) -> str | None:
    """Put back the previous saved version of your copy of *name*; returns its timestamp."""
    target = user_dir() / f"{name}.md"
    current = target.read_text(encoding="utf-8") if target.exists() else None
    for old in reversed(sorted(_history_dir().glob(f"{name}@*.md"))):
        text = old.read_text(encoding="utf-8")
        if text != current:
            if current is not None:
                snapshot_overrides()
            target.write_text(text, encoding="utf-8")
            reload()
            ts = old.stem.split("@", 1)[1]
            return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
    return None
