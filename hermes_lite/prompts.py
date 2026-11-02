"""System prompt: Hermes Agent's prompt blocks, assembled in Hermes' order (stable → context → volatile).

The guidance text below is taken from Hermes Agent (agent/prompt_builder.py, agent/coding_context.py),
trimmed only where it names tools or surfaces Hermes Lite does not have.
"""

from __future__ import annotations

import datetime as dt
import re
import os
import platform
import subprocess
from pathlib import Path

from . import promptfiles as pf
from .config import Config
from .tools import REGISTRY, ToolContext
from .tools.memory import MemoryStore
from .tools.skills import skills_index

STEER_OPEN = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
              "output and not a new delivery when replayed from conversation history]")
STEER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"

PROJECT_MARKERS = (".git", "pyproject.toml", "setup.py", "requirements.txt", "package.json", "Cargo.toml", "go.mod",
                   "pom.xml", "build.gradle", "CMakeLists.txt", "Makefile", "meson.build", "deno.json", "composer.json")


def _git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=3, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def find_git_root(cwd: Path) -> Path | None:
    return next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), None)


def workspace_root(cwd: Path) -> Path | None:
    git_root = find_git_root(cwd)
    if git_root:
        return git_root
    return cwd if any((cwd / m).exists() for m in PROJECT_MARKERS) else None


def workspace_block(cwd: Path) -> str:
    root = workspace_root(cwd)
    if root is None:
        return ""
    lines = ["Workspace (snapshot at session start — re-check with `git` before acting on it):", f"- Root: {root}"]
    if (root / ".git").exists():
        branch = _git(root, "branch", "--show-current") or "(detached HEAD)"
        lines.append(f"- Branch: {branch}")
        status = _git(root, "status", "--porcelain")
        changed = len(status.splitlines()) if status else 0
        lines.append(f"- Status: {f'{changed} changed/untracked files' if changed else 'clean'}")
        recent = _git(root, "log", "-3", "--pretty=%h %s")
        if recent:
            lines += ["- Recent commits:", *(f"    {c}" for c in recent.splitlines())]
    manifests = [m for m in PROJECT_MARKERS if m != ".git" and (root / m).exists()]
    if manifests:
        lines.append(f"- Project: {', '.join(manifests[:6])}")
    return "\n".join(lines)


def _read_capped(path: Path, cap: int = 20000) -> str:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if len(text) > cap:
        text = text[: int(cap * 0.7)] + f"\n\n[... truncated; read the full file at {path} ...]\n\n" + text[-int(cap * 0.2):]
    return text


def context_files_block(cwd: Path, home: Path) -> str:
    """Hermes' discovery order — first project context type wins — plus SOUL.md handled by identity."""
    stop = find_git_root(cwd)
    chain = [cwd, *cwd.parents] if stop else [cwd]
    sections: list[str] = []
    for d in chain:
        hit = next((d / n for n in (".hermes.md", "HERMES.md") if (d / n).is_file()), None)
        if hit:
            sections.append(f"## {hit.name}\n\n{_read_capped(hit)}")
            break
        if d == stop:
            break
    if not sections:
        agents = [d / "AGENTS.md" for d in reversed(chain[: chain.index(stop) + 1] if stop else chain)
                  if (d / "AGENTS.md").is_file()]
        sections += [f"## {p.relative_to(stop) if stop else p.name}\n\n{_read_capped(p)}" for p in agents]
    if not sections:
        for name in ("CLAUDE.md", ".cursorrules"):
            if (cwd / name).is_file():
                sections.append(f"## {name}\n\n{_read_capped(cwd / name)}")
                break
    if not sections:
        return ""
    return ("# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n"
            + "\n\n".join(sections))


def working_notes_block(cwd: Path, home: Path, ctx: ToolContext, off: set) -> str:
    """The model's own notes (notes tool) and the todo items left open in an earlier session."""
    from .tools import notes as nt
    items = nt.read(nt.notes_file(str(cwd), home)) if "notes" not in off else []
    todos = ctx.state.get("todos") or []
    open_items = [t for t in todos if t.get("status") in ("pending", "in_progress")] if ctx.state.get("todos_restored") else []
    if not items and not open_items:
        return ""
    todo_text = ("\nUnfinished todo items from your last session here (todo_list shows and updates them):\n"
                 + "\n".join(f"- [{t['status']}] {t['content']}" for t in open_items)) if open_items else ""
    return pf.get("notes_block", notes=nt.render(items) or "(none yet)", path=str(nt.notes_file(str(cwd), home)),
                  todos=todo_text).strip()


def skills_block(ctx: ToolContext) -> str:
    index = skills_index(ctx)
    return pf.get("skills_block", skills=index) if index else ""


def _distro() -> str:
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if platform.system() == "Windows":
        return f"Windows {platform.version()}"
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return ""


def _powershell(cfg: Config) -> bool:
    from .oscompat import IS_WINDOWS
    return IS_WINDOWS or cfg.get_path("agent.shell") == "powershell"


def _shell_line(cfg: Config) -> str:
    from .oscompat import shell_name
    if _powershell(cfg):
        name = shell_name() if platform.system() == "Windows" else "PowerShell (pwsh)"
        return (f"Shell: {name} (local machine — commands run directly on the user's computer). Use PowerShell "
                "syntax and cmdlets: Get-ChildItem, Get-Content, Select-String, Get-FileHash, Get-Date, "
                "$env:NAME = 'value', `;` between commands. Unix tools (grep, sed, ls -la, export, chmod) "
                "are usually not installed.")
    if platform.system() == "Darwin":
        return ("Shell: bash on macOS (local machine — commands run directly on the user's computer). BSD "
                "tools: sed -i '' …, no GNU-only flags; install tools with brew.")
    return "Shell: bash (local machine — commands run directly on the user's computer)"


def environment_block(cfg: Config, ctx: ToolContext) -> str:
    host = f"{platform.system()} ({platform.release()})"
    distro = _distro()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or ("Hyprland" if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") else "")
    lines = [
        f"Host: {host}" + (f", {distro}" if distro else "") + (f", desktop: {desktop}" if desktop else ""),
        _shell_line(cfg),
        f"User home directory: {Path.home()}",
        f"Current working directory: {ctx.cwd}",
        # no session id here: an identical system prompt lets llama-server reuse its saved cache
        f"Scratch directory: {'$env:TEMP' if _powershell(cfg) else '$TMPDIR'} (a per-session folder under "
        f"{ctx.home / 'scratch'}; write temporary files "
        "and probes there, never under the system temp dir)",
        *(["Python: every command runs with the project's virtualenv active (.venv in the project folder or a "
           "parent); the first pip install creates it. Just run python / pip install — no activate, no sudo pip, "
           "no --break-system-packages."] if cfg.get_path("agent.auto_venv", True) else []),
        f"Model: {cfg.get_path('model.name')} running locally through llama.cpp, "
        f"{cfg.get_path('model.context_length')} token context window.",
    ]
    return "# Hermes runtime environment\n\n" + "\n".join(lines) + "\n\n<!-- End Hermes runtime environment -->"


def build_system_prompt(cfg: Config, ctx: ToolContext, memory: MemoryStore) -> str:
    home = ctx.home
    cwd = Path(ctx.cwd)
    from .tools.base import disabled_tools
    off = disabled_tools(cfg)
    names = set(REGISTRY) - off
    soul = home / "SOUL.md"
    identity = soul.read_text(encoding="utf-8-sig").strip() if soul.is_file() else pf.get("identity")
    stable = [identity, pf.get("task_completion"), pf.get("parallel_tool_calls")]
    stable.append(" ".join(g for g in (pf.get("memory_guidance") if "memory" in names else "",
                                        pf.get("skills_guidance") if "skill_manage" in names else "") if g))
    stable += [pf.get("steering", steer_open=STEER_OPEN, steer_close=STEER_CLOSE), pf.get("tool_use"),
               pf.get("execution")]
    from .loopguard import settings as guard_settings
    if guard_settings(cfg):
        stable.append(pf.get("loop_guard_line"))
    ws = workspace_block(cwd)
    if ws:
        stable.append(pf.get("coding"))
    from . import projectnotes
    notes_file = projectnotes.notes_path(cfg, cwd)
    notes = projectnotes.for_prompt(notes_file)
    notes_block = pf.get("project_notes_block", notes=notes, path=str(notes_file)) if notes else ""
    context = [context_files_block(cwd, home), notes_block, working_notes_block(cwd, home, ctx, off), ws,
               pf.get("tui_platform")]
    started = dt.datetime.now().astimezone()
    volatile = [skills_block(ctx), memory.block("memory"), memory.block("user"),
                f"Conversation started: {started.strftime('%A, %B %d, %Y')} ({started.tzname()})",
                environment_block(cfg, ctx)]
    stable = [_without_tools(p, off) for p in stable]
    return "\n\n".join(p.strip() for p in (*stable, *context, *volatile) if p and p.strip())


def _without_tools(text: str, off: set[str]) -> str:
    """Drop guidance bullets that point the model at a tool you switched off."""
    if not off or not text:
        return text
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in sorted(off)) + r")(?![\w-])")
    return "\n".join(line for line in text.split("\n")
                     if not (line.lstrip().startswith(("-", "*")) and pat.search(line)))
