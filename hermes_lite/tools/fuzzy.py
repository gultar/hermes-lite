"""Find-and-replace with layered fuzzy matching (ported from Hermes Agent's patch tool), plus the
repairs small models need: line-number prefixes copied from read_file, escape drift (doubled
backslashes, \\' quoting), and a "closest lines" hint when nothing matches.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Callable, Optional

Span = tuple[int, int]
LINE_NO = re.compile(r"^\s*\d+\|")


# ── helpers ─────────────────────────────────────────────────────────────
def _line_offsets(content: str) -> list[int]:
    offs, pos = [], 0
    for line in content.splitlines(keepends=True):
        offs.append(pos)
        pos += len(line)
    offs.append(pos)
    return offs


def _block_spans(content: str, n: int, accept: Callable[[int], bool], keep_final_newline: bool) -> list[Span]:
    lines = content.splitlines(keepends=True)
    offs = _line_offsets(content)
    spans = []
    for i in range(len(lines) - n + 1):
        if accept(i):
            end = offs[i + n]
            if not keep_final_newline and content[end - 1:end] == "\n":
                end -= 1
            spans.append((offs[i], end))
    return spans


def _by_lines(norm: Callable[[str], str]) -> Callable[[str, str], list[Span]]:
    def strategy(content: str, old: str) -> list[Span]:
        want = [norm(l) for l in old.strip("\n").splitlines()]
        if not want or not any(want):
            return []
        have = [norm(l.rstrip("\r\n")) for l in content.splitlines(keepends=True)]
        n = len(want)
        return _block_spans(content, n, lambda i: have[i:i + n] == want, old.endswith("\n"))
    return strategy


def _exact(content: str, old: str) -> list[Span]:
    return [m.span() for m in re.finditer(re.escape(old), content)] if old else []


UNICODE_MAP = {"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-",
               "…": "...", " ": " ", "−": "-"}


def _ascii(s: str) -> str:
    return "".join(UNICODE_MAP.get(ch, ch) for ch in unicodedata.normalize("NFC", s))


def _block_anchor(content: str, old: str) -> list[Span]:
    """First and last lines equal (trimmed), middle similar enough."""
    pat = _ascii(old.strip("\n")).splitlines()
    if len(pat) < 3:
        return []
    lines = [_ascii(l) for l in content.splitlines()]
    n, first, last = len(pat), pat[0].strip(), pat[-1].strip()
    cands = [i for i in range(len(lines) - n + 1) if lines[i].strip() == first and lines[i + n - 1].strip() == last]
    threshold = 0.5 if len(cands) == 1 else 0.7
    middle = "\n".join(pat[1:-1])

    def accept(i: int) -> bool:
        return i in cands and SequenceMatcher(None, "\n".join(lines[i + 1:i + n - 1]), middle).ratio() >= threshold
    return _block_spans(content, n, accept, old.endswith("\n"))


def _context_aware(content: str, old: str) -> list[Span]:
    """Every non-blank line at least 80% similar to the file's line (3+ lines only: a single similar
    line is too easy to confuse with the wrong one)."""
    pat = old.strip("\n").splitlines()
    lines = content.splitlines()
    n = len(pat)
    if n < 3 or n > len(lines):
        return []

    def sim(a: str, b: str) -> float:
        return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()

    def accept(i: int) -> bool:
        block = lines[i:i + n]
        return all(not p.strip() or sim(p.strip(), c.strip()) >= 0.8 for p, c in zip(pat, block))
    return _block_spans(content, n, accept, old.endswith("\n"))


STRATEGIES: list[tuple[str, Callable[[str, str], list[Span]]]] = [
    ("exact", _exact),
    ("trailing-whitespace", _by_lines(lambda s: s.rstrip())),
    ("indentation", _by_lines(lambda s: s.strip())),
    ("whitespace-collapsed", _by_lines(lambda s: re.sub(r"\s+", " ", s).strip())),
    ("unicode-normalised", _by_lines(lambda s: re.sub(r"\s+", " ", _ascii(s)).strip())),
    ("block-anchor", _block_anchor),
    ("similar-lines", _context_aware),
]
APPROXIMATE = {"block-anchor", "similar-lines"}


def find_spans(content: str, old: str) -> tuple[str, list[Span]]:
    for name, fn in STRATEGIES:
        spans = fn(content, old)
        if spans:
            return name, spans
    return "", []


# ── repairs for small-model slips ───────────────────────────────────────
def _strip_line_numbers(s: str) -> Optional[str]:
    lines = s.splitlines(keepends=True)
    body = [l for l in lines if l.strip()]
    if body and all(LINE_NO.match(l) for l in body):
        return "".join(LINE_NO.sub("", l, count=1) for l in lines)
    return None


def _halve_backslashes(s: str) -> str:
    return re.sub(r"\\+", lambda m: "\\" * (len(m.group(0)) // 2), s)


def _even_backslashes(s: str) -> bool:
    return all(len(r) % 2 == 0 for r in re.findall(r"\\+", s))


def _repairs(content: str, old: str, new: str) -> list[tuple[str, str, str]]:
    """Candidate (old, new, note) pairs, each applied consistently to both strings."""
    out = []
    stripped = _strip_line_numbers(old)
    if stripped is not None:
        new2 = _strip_line_numbers(new)
        out.append((stripped, new2 if new2 is not None else new,
                    "removed the 'N|' line-number prefixes copied from read_file"))
    if "\\n" in old and "\n" not in old:
        out.append((old.replace("\\n", "\n").replace("\\t", "\t"), new.replace("\\n", "\n").replace("\\t", "\t"),
                    "turned literal \\n into real line breaks"))
    if "\\\\" in old and _even_backslashes(old) and _halve_backslashes(old) in content:
        if _even_backslashes(new):
            out.append((_halve_backslashes(old), _halve_backslashes(new),
                        "backslashes were doubled in both strings; halved them — check the diff"))
    for q in ("'", '"', "`"):
        esc = "\\" + q
        if esc in old and old.replace(esc, q) in content:
            out.append((old.replace(esc, q), new.replace(esc, q),
                        f"removed stray backslashes before {q} — check the diff"))
    return out


# ── reindent and closest-lines hint ─────────────────────────────────────
def _reindent(new: str, old: str, matched: str) -> str:
    def first_indent(s: str) -> str:
        for line in s.splitlines():
            if line.strip():
                return line[: len(line) - len(line.lstrip())]
        return ""
    have, want = first_indent(matched), first_indent(old)
    if have == want:
        return new
    out = []
    for line in new.splitlines(keepends=True):
        if line.strip() and want and line.startswith(want):
            line = have + line[len(want):]
        elif line.strip() and not want:
            line = have + line
        out.append(line)
    return "".join(out)


def closest_lines(content: str, old: str, context: int = 2, max_results: int = 3) -> str:
    old_lines = [l for l in old.splitlines() if l.strip()]
    if not old_lines:
        return ""
    anchor = LINE_NO.sub("", old_lines[0], count=1).strip()
    lines = content.splitlines()
    scored = sorted(((SequenceMatcher(None, anchor, l.strip()).ratio(), i) for i, l in enumerate(lines) if l.strip()),
                    key=lambda x: -x[0])
    parts, seen = [], set()
    for score, i in scored[:max_results]:
        if score < 0.4:
            break
        a, b = max(0, i - context), min(len(lines), i + len(old.splitlines()) + context)
        if (a, b) in seen:
            continue
        seen.add((a, b))
        parts.append("\n".join(f"{a + j + 1}|{lines[a + j]}" for j in range(b - a)))
    return "\n---\n".join(parts)


# ── entry point ─────────────────────────────────────────────────────────
def apply(content: str, old: str, new: str, replace_all: bool) -> tuple[str, str, int]:
    """Returns (new_content, strategy[+note], count). Raises ValueError with a model-facing message."""
    if old == new:
        raise ValueError("new_string is identical to old_string; nothing to change.")
    if not old:
        raise ValueError("old_string is empty. Give the exact text to replace, or use start_line/end_line.")
    attempts = [(old, new, "")] + _repairs(content, old, new)
    precise = [st for st in STRATEGIES if st[0] not in APPROXIMATE]
    approximate = [st for st in STRATEGIES if st[0] in APPROXIMATE]
    # Precise matching (with every repair) first; approximate matching only as a last resort, so a loose
    # match can never win over a repair that explains the difference exactly.
    for phase in (precise, approximate):
        for o, n, note in attempts:
            strategy, spans = "", []
            for name, fn in phase:
                spans = fn(content, o)
                if spans:
                    strategy = name
                    break
            if not spans:
                continue
            if strategy in APPROXIMATE and (len(spans) > 1 or replace_all):
                continue  # approximate matches are only trusted when unique
            if len(spans) > 1 and not replace_all:
                lines_at = [content.count("\n", 0, s) + 1 for s, _ in spans]
                raise ValueError(f"old_string matches {len(spans)} places (lines {', '.join(map(str, lines_at[:10]))}). "
                                 "Add surrounding lines to make it unique, set replace_all=true, or use "
                                 "start_line/end_line.")
            out, last = [], 0
            for s_, e in spans:
                repl = n if strategy == "exact" else _reindent(n, o, content[s_:e])
                out += [content[last:s_], repl]
                last = e
            out.append(content[last:])
            label = strategy + (" (approximate — check the diff)" if strategy in APPROXIMATE else "")
            return "".join(out), label + (f"; {note}" if note else ""), len(spans)
    hint = closest_lines(content, old)
    raise ValueError("old_string was not found in the file."
                     + (f" The closest lines in the file are:\n{hint}\n" if hint else " ")
                     + "Easiest fix: call patch again with start_line and end_line (the line numbers shown above "
                       "or by read_file) and new_string, instead of old_string.")
