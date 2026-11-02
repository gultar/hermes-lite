"""Context compaction with Hermes' structured checkpoint summary."""

from __future__ import annotations

import datetime as dt
import json

from . import promptfiles as pf

CHARS_PER_TOKEN = 3.2

def _text(content) -> str:
    if isinstance(content, list):  # multimodal parts: only the text counts here (images are added apart)
        return " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or "")


def estimate_tokens(messages: list[dict], reasoning: bool = False) -> int:
    """Rough token count of what is SENT. Past thinking is not sent back (model.replay_reasoning), so
    it is only counted when *reasoning* is true."""
    chars = 0
    for m in messages:
        chars += len(_text(m.get("content")))
        if reasoning:
            chars += len(str(m.get("reasoning_content") or ""))
        for tc in m.get("tool_calls") or []:
            chars += len(json.dumps(tc))
    return int(chars / CHARS_PER_TOKEN)


def split_point(messages: list[dict], keep_tokens: int) -> int:
    """Index where the verbatim tail starts: never inside an assistant→tool-results group."""
    budget, i = keep_tokens * CHARS_PER_TOKEN, len(messages)
    used = 0.0
    while i > 1:
        cand = messages[i - 1]
        used += len(str(cand.get("content") or "")) + sum(len(json.dumps(tc)) for tc in cand.get("tool_calls") or [])
        if used > budget:
            break
        i -= 1
    while i < len(messages) and messages[i].get("role") == "tool":
        i += 1  # tail must not start with orphaned tool results
    return i


def serialize(messages: list[dict], per_item: int = 2500) -> str:
    out = []
    for m in messages:
        role = m.get("role")
        content = _text(m.get("content"))
        # what the user typed matters most; old tool output least (the facts list keeps the file names)
        limit = 6000 if role == "user" and not m.get("_synthetic") else 1200 if role == "tool" else per_item
        if len(content) > limit:
            content = content[: limit // 2] + "\n[...]\n" + content[-limit // 2:]
        if role == "assistant" and m.get("tool_calls"):
            calls = "; ".join(f"{tc['function']['name']}({tc['function']['arguments'][:400]})" for tc in m["tool_calls"])
            out.append(f"ASSISTANT: {content}\n  [tool calls: {calls}]")
        elif role == "tool":
            out.append(f"TOOL RESULT ({m.get('name', '')}): {content}")
        else:
            out.append(f"{role.upper()}: {content}")
    return "\n\n".join(out)


def fit(middle: list[dict], max_chars: int | None) -> str:
    """serialize(), squeezed to *max_chars*: shorter items first, then the oldest turns go (the
    first one, usually the task, stays)."""
    text = serialize(middle)
    if not max_chars or len(text) <= max_chars:
        return text
    for per_item in (1200, 600, 300):
        text = serialize(middle, per_item)
        if len(text) <= max_chars:
            return text
    rest = list(middle[1:])
    while rest and len(text) > max_chars:
        rest = rest[max(1, len(rest) // 8):]
        text = serialize(middle[:1], 300) + "\n\n[… older turns left out to fit …]\n\n" + serialize(rest, 300)
    return text[-max_chars:]


def summary_request(middle: list[dict], previous_summary: str = "", budget: int = 1800,
                    max_chars: int | None = None) -> list[dict]:
    today = dt.date.today().strftime("%A, %B %d, %Y")
    prior = f"\n\nPREVIOUS SUMMARY (update it with the new turns; keep what still matters):\n{previous_summary}" if previous_summary else ""
    turns = fit(middle, max_chars - len(prior) - 6000 if max_chars else None)
    prompt = ("Create a structured checkpoint summary for the conversation after earlier turns are compacted. The "
              "summary should preserve enough detail for continuity without re-reading the original turns.\n\n"
              f"TURNS TO SUMMARIZE:\n{turns}{prior}\n\nUse this exact structure:\n\n"
              + pf.get("compaction_template", budget=budget, today=today))
    return [{"role": "system", "content": "You summarize agent transcripts into precise handoff notes."},
            {"role": "user", "content": prompt}]


# ── trimming old tool output (before any summary) ───────────────────────
PRUNE_MIN_CHARS = 1500     # tool results shorter than this stay as they are
BIG_ARGS = ("content", "old_string", "new_string")


def _stub(name: str, content: str) -> str:
    note = "removed from the conversation to save context; run the tool again if you need it"
    try:
        d = json.loads(content)
    except ValueError:
        d = None
    if isinstance(d, dict) and name == "read_file" and d.get("path"):
        return json.dumps({"path": d["path"], "total_lines": d.get("total_lines"),
                           "trimmed": "The file content shown here earlier was " + note.replace("run the tool",
                                      "read_file the part you need") + " (the file may have changed since)."})
    head, tail = content[:500], content[-300:]
    return f"{head}\n[… {len(content) - 800} characters of this old tool output were {note} …]\n{tail}"


def _slim_args(arguments: str) -> str | None:
    """write_file/patch calls carry whole files in their arguments: keep the call, drop the bulk."""
    try:
        d = json.loads(arguments)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    changed = False
    for key in BIG_ARGS:
        v = d.get(key)
        if isinstance(v, str) and len(v) > 800:
            d[key] = (f"[{v.count(chr(10)) + 1} lines / {len(v)} characters, removed from the conversation to "
                      "save context — the file on disk has the result; read it if you need it]")
            changed = True
    return json.dumps(d, ensure_ascii=False) if changed else None


def prune(messages: list[dict], start: int, end: int, protect: frozenset | set = frozenset(),
          extra: frozenset | set = frozenset()) -> tuple[int, int]:
    """Shrink big tool results and big write/patch arguments in messages[start:end] (plus the indexes
    in *extra*, wherever they are), in place, skipping the indexes in *protect*. Everything else (what
    the user said, what the model said, which tools it called on what) stays word for word.
    Returns (items trimmed, characters freed)."""
    items = freed = 0
    for i in sorted(set(range(start, end)) | set(extra)):
        if i in protect or not 0 < i < len(messages):
            continue
        m = messages[i]
        if m.get("role") == "tool" and not m.get("_pruned"):
            content = _text(m.get("content"))
            if len(content) > PRUNE_MIN_CHARS:
                new = _stub(str(m.get("name") or ""), content)
                if len(new) < len(content):
                    m["content"], m["_pruned"] = new, True
                    items, freed = items + 1, freed + len(content) - len(new)
        elif m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = str(fn.get("arguments") or "")
                if len(args) > PRUNE_MIN_CHARS and (slim := _slim_args(args)) and len(slim) < len(args):
                    fn["arguments"] = slim
                    items, freed = items + 1, freed + len(args) - len(slim)
    return items, freed
