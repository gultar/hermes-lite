"""Tool registry and the per-session context tools run against."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import Config


def ok(**data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def error(message: str, **data: Any) -> str:
    return json.dumps({"success": False, "error": message, **data}, ensure_ascii=False)


@dataclass
class Tool:
    name: str
    schema: dict
    handler: Callable[[dict, "ToolContext"], str]
    emoji: str = "⚙"
    read_only: bool = False          # read-only tools may run concurrently
    label: Callable[[dict], str] = lambda a: ""  # one-line summary for the UI card
    requires: str = ""               # "vision": offered only when the server can take images

    def openai_schema(self) -> dict:
        """The schema the model sees; its description comes from prompts/tool_<name>.md when present."""
        from .. import promptfiles
        try:
            description = promptfiles.get(promptfiles.TOOL_PREFIX + self.name)
        except RuntimeError:
            description = self.schema.get("description", "")
        schema = {**self.schema, "description": description}
        short = promptfiles.param_descriptions(self.name)
        props = (schema.get("parameters") or {}).get("properties")
        if short and isinstance(props, dict):
            import copy
            schema["parameters"] = copy.deepcopy(schema["parameters"])
            for key, text in short.items():
                if key in schema["parameters"]["properties"] and isinstance(text, str):
                    schema["parameters"]["properties"][key]["description"] = text
        return {"type": "function", "function": schema}


@dataclass
class ToolContext:
    """Everything a tool needs. UI hooks are plain callables that may block (tools run in threads)."""
    cfg: Config
    home: Path
    cwd: str
    session_id: str
    approve: Callable[[str, str], str] = lambda command, reason: "once"   # -> once|session|deny
    clarify: Callable[[list], list] = lambda questions: []
    # (prompt, command) -> (password or None, remember_for_session). None = no UI to ask (headless).
    ask_secret: Optional[Callable[[str, str], tuple]] = None
    progress: Callable[[str], None] = lambda text: None                  # live output into the tool card
    notify: Callable[[str], None] = lambda text: None                    # background-process notices
    state: dict = field(default_factory=dict)                             # per-session tool state

    def resolve(self, path: str) -> Path:
        p = Path(path or ".").expanduser()
        return p if p.is_absolute() else Path(self.cwd) / p


def trash_dirs() -> list[Path]:
    import os
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return [data / "Trash", home / ".Trash"]


def in_trash(path: str | Path) -> bool:
    """True for anything in the desktop's Trash (deleted, not gone): ~/.local/share/Trash, a
    removable drive's .Trash-1000, macOS ~/.Trash, Windows' $Recycle.Bin."""
    import os
    try:
        p = Path(os.path.abspath(os.path.expanduser(str(path))))
    except (OSError, ValueError):
        return False
    if any(part.startswith(".Trash-") or part in (".Trash", "$Recycle.Bin") for part in p.parts):
        return True
    return any(p == t or t in p.parents for t in trash_dirs())


REGISTRY: dict[str, Tool] = {}


def disabled_tools(cfg: Config) -> set[str]:
    """Tools you switched off in config.yaml (agent.disabled_tools): not offered, not described."""
    off = {str(n).strip() for n in (cfg.get_path("agent.disabled_tools", []) or [])}
    from .. import promptfiles
    if promptfiles.STYLE == "compact":
        off.add("skills_list")  # the skills index is in the system prompt; skill_view loads one
    return off


def schema_without(tool: Tool, off: set[str]) -> dict:
    """The tool's schema with sentences that point at switched-off tools removed."""
    import copy
    import re
    schema = tool.openai_schema()
    others = off - {tool.name}
    if not others:
        return schema
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in sorted(others)) + r")(?![\w-])")

    def clean(text: str) -> str:
        parts = re.split(r"(?<=[.!?])\s+", text)
        kept = [x for x in parts if not pat.search(x)]
        return " ".join(kept) if kept else text

    schema = copy.deepcopy(schema)
    fn = schema["function"]
    fn["description"] = clean(fn.get("description", ""))
    for prop in (fn.get("parameters", {}).get("properties") or {}).values():
        if isinstance(prop, dict) and isinstance(prop.get("description"), str):
            prop["description"] = clean(prop["description"])
    return schema


def register(tool: Tool) -> Tool:
    REGISTRY[tool.name] = tool
    return tool


def short(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def truncate_middle(text: str, limit: int, save_dir: Optional[Path] = None, stem: str = "output") -> tuple[str, Optional[str]]:
    """Head+tail truncate; the full text is saved to a file whose path is returned."""
    if len(text) <= limit:
        return text, None
    saved = None
    if save_dir is not None:
        import time
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"{stem}-{int(time.time() * 1000)}.txt"
        path.write_text(text, encoding="utf-8", errors="replace")
        saved = str(path)
    head, tail = int(limit * 0.4), int(limit * 0.6)
    omitted = len(text) - head - tail
    note = f"\n\n[... {omitted} characters omitted"
    note += f"; full output saved to {saved} — page through it with read_file ...]\n\n" if saved else " ...]\n\n"
    return text[:head] + note + text[-tail:], saved
