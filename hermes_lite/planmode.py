"""/plan and /build.

Plan mode: the model investigates (read, search, docs, read-only commands) and writes a plan; it
cannot change anything. The tool list stays the same (so llama-server's cached system prompt stays
valid); editing tools are refused while planning.

The plan reaches the build four ways, because the model's thinking is never carried over:
  1. it is the visible reply, in fixed sections, with the findings spelled out
  2. it is saved to <project>/.hermes/plan.md (survives compaction, /new, restarts; you can edit it)
  3. /build sends the whole file as the build instruction and turns its steps into the todo list
  4. compaction re-attaches it to the summary while the build runs"""

from __future__ import annotations

import re
import time
from pathlib import Path

PLAN_FILE = Path(".hermes") / "plan.md"

WRITE_TOOLS = {"write_file", "patch", "skill_manage"}

# Commands that only look. Anything else needs the user's approval while planning.
READ_ONLY = re.compile(
    r"^\s*(ls|ll|la|dir|tree|pwd|cat|bat|head|tail|less|more|wc|file|stat|du|df|free|uname|whoami|id|date|"
    r"which|whereis|type|command\s+-v|echo|printf|env|printenv|grep|rg|ag|ack|find|fd|locate|jq|yq|sort|uniq|"
    r"cut|tr|column|diff|cmp|md5sum|sha\d*sum|ps|pgrep|top\s+-b|lsblk|lscpu|lspci|lsusb|nvidia-smi|ip\s+a|"
    r"ss\s+-|netstat|hostname|realpath|readlink|basename|dirname|nproc|"
    r"git\s+(status|log|diff|show|branch|remote|blame|ls-files|rev-parse|describe|shortlog|tag\s*$|"
    r"config\s+--get|stash\s+list)|"
    r"(python3?|node|npm|pnpm|yarn|pip3?|uv|cargo|rustc|go|java|ruby|php|deno|bun|gcc|clang|make|cmake)"
    r"\s+(--version|-v|-V)\b|"
    r"npm\s+(ls|list|view|outdated|why)|pip3?\s+(list|show|freeze)|uv\s+pip\s+(list|show|freeze)|"
    r"cargo\s+(tree|metadata)|pacman\s+-Q|paru\s+-Q|dpkg\s+-l|apt\s+list|brew\s+(list|info)|"
    r"Get-ChildItem|gci|Get-Content|gc|Get-Item|Get-Location|Select-String|sls|Test-Path|Get-Command|"
    r"Get-Process|Get-Date|Get-FileHash|Measure-Object|Get-Service|Resolve-Path)\b", re.I)
WRITES = re.compile(r"(?<![0-9&])>(?!&|\s*/dev/null|\s*\$null)|>>|\btee\b|\$\(|`|\bsed\s+-i|(?<!\w)-(delete|exec|execdir|ok|fprint\w*)\b|"
                    r"\b(rm|mv|cp|mkdir|touch|chmod|chown|ln|kill|pkill|sudo|curl|wget)\b|"
                    r"\b(Set-|New-|Remove-|Move-|Copy-|Rename-|Out-File|Add-Content|Stop-|Start-)", re.I)


def read_only_command(command: str) -> bool:
    """True when every part of the command line only looks (ls, git status, grep…)."""
    if WRITES.search(command):
        return False
    parts = [p for p in re.split(r"\|\||&&|;|\|", command) if p.strip()]
    return bool(parts) and all(READ_ONLY.match(p) for p in parts)


def plan_path(cwd: str) -> Path:
    return Path(cwd) / PLAN_FILE


def looks_like_plan(text: str) -> bool:
    low = text.lower()
    return "## steps" in low and ("## goal" in low or "## findings" in low)


def save_plan(cwd: str, task: str, text: str) -> Path:
    path = plan_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"<!-- Hermes Lite plan · {time.strftime('%Y-%m-%d %H:%M')} · task: {' '.join(task.split())[:200]} -->\n"
    path.write_text(header + text.strip() + "\n", encoding="utf-8")
    return path


def load_plan(cwd: str) -> str | None:
    try:
        text = plan_path(cwd).read_text(encoding="utf-8")
    except OSError:
        return None
    text = re.sub(r"^<!--.*?-->\s*", "", text, flags=re.S)
    return text.strip() or None


def steps(plan: str) -> list[str]:
    """The numbered items of the plan's Steps section."""
    m = re.search(r"^#+\s*steps\b.*?$(.*?)(?=^#+\s|\Z)", plan, re.I | re.M | re.S)
    body = m.group(1) if m else plan
    items, current = [], None
    for line in body.splitlines():
        if re.match(r"^\s*\d+[.)]\s+", line):
            if current:
                items.append(current)
            current = re.sub(r"^\s*\d+[.)]\s+", "", line).strip()
        elif current is not None and line.strip() and line.startswith((" ", "\t")):
            continue  # sub-bullets stay in the plan text; the todo item is the step's first line
    if current:
        items.append(current)
    clean = [re.sub(r"\*\*(.*?)\*\*", r"\1", s).strip() for s in items]
    return [s if len(s) <= 140 else s[:139] + "…" for s in clean if s]
