"""Facts about the session that compaction must never lose, kept by code instead of by the model.

The model's checkpoint summary is good at the story but small models drop details: the exact words
of a request, which files they created, the last command that failed. This log records those as
they happen and is attached, word for word, to every compaction handoff (agent.work_ledger)."""

from __future__ import annotations

import json
import os
from pathlib import Path

REQUEST_CHARS = 1500      # per request (head + tail kept when longer)
REQUESTS_CHARS = 6000     # all compacted requests together
COMMANDS_KEPT = 8


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 400] + " […] " + text[-380:]


class WorkLog:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, int]] = {}   # path -> {"read": n, "created": n, "edited": n}
        self.commands: list[tuple[str, object]] = []  # (command, exit code)
        self.requests: list[str] = []                 # the user's messages that were compacted away

    # ── recording ───────────────────────────────────────────────────────
    def record(self, name: str, args: dict | None, result: str, cwd: str) -> None:
        if not isinstance(args, dict):
            return
        try:
            d = json.loads(result)
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        failed = d.get("success") is False or bool(d.get("error"))
        if name in ("read_file", "write_file", "patch") and not failed:
            raw = str(d.get("path") or args.get("path") or "")
            if not raw:
                return
            path = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(cwd, raw))
            rec = self.files.setdefault(path, {})
            kind = {"read_file": "read", "patch": "edited"}.get(name) or ("created" if d.get("created") else "edited")
            rec[kind] = rec.get(kind, 0) + 1
        elif name == "terminal" and args.get("command"):
            code = d.get("exit_code", "?") if d else "?"
            if d.get("session_id") and d.get("exit_code") is None:
                code = f"background {d['session_id']}"
            self.commands.append((" ".join(str(args["command"]).split())[:200], code))
            self.commands = self.commands[-COMMANDS_KEPT:]

    def add_requests(self, messages: list[dict]) -> None:
        """The user's own messages from the part being compacted."""
        for m in messages:
            if m.get("role") == "user" and not m.get("_synthetic"):
                text = str(m.get("_typed") or m.get("content") or "")
                if text.strip():
                    self.requests.append(_clip(text, REQUEST_CHARS))

    # ── the handoff text ────────────────────────────────────────────────
    def render(self, cwd: str, last_said: str = "") -> str:
        parts = ["## Facts kept by Hermes Lite (recorded as they happened — exact, not summarized)"]
        if self.requests:
            # the first request (the task) and the most recent ones, within the budget
            chosen, used = [], 0
            for i in [0, *range(len(self.requests) - 1, 0, -1)]:
                text = self.requests[i]
                if used + len(text) > REQUESTS_CHARS and chosen:
                    break
                chosen.append(i)
                used += len(text)
            chosen.sort()
            lines = ["### What the user asked in the compacted part (their words)"]
            for n, i in enumerate(chosen):
                if n and i != chosen[n - 1] + 1:
                    lines.append("(… earlier messages left out …)")
                lines.append(f"{i + 1}. " + self.requests[i].replace("\n", "\n   "))
            parts.append("\n".join(lines))
        if self.files:
            base = Path(cwd)
            lines = ["### Files touched in this session"]
            for path, rec in list(self.files.items())[-40:]:
                p = Path(path)
                try:
                    shown = str(p.relative_to(base))
                except ValueError:
                    shown = path
                did = ", ".join(f"{k} {v}×" if v > 1 else k for k, v in rec.items() if v)
                try:
                    with open(p, "rb") as fh:
                        state = f"now {sum(1 for _ in fh)} lines"
                except FileNotFoundError:
                    state = "no longer exists"
                except OSError:
                    state = "unreadable now"
                lines.append(f"- {shown} — {did} ({state})")
            parts.append("\n".join(lines))
        if self.commands:
            parts.append("### Last commands\n" + "\n".join(f"- `{c}` → exit {code}" for c, code in self.commands))
        if last_said.strip():
            parts.append("### Your last message before the compaction\n" + _clip(last_said, 900))
        return "\n\n".join(parts) if len(parts) > 1 else ""

    # ── persistence ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"files": self.files, "commands": self.commands, "requests": self.requests}

    @classmethod
    def from_dict(cls, data: dict | None) -> "WorkLog":
        log = cls()
        if isinstance(data, dict):
            log.files = dict(data.get("files") or {})
            log.commands = [tuple(c) for c in data.get("commands") or []]
            log.requests = list(data.get("requests") or [])
        return log
