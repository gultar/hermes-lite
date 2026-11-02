"""read_file, write_file, patch, search_files."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import fuzzy
from .base import Tool, ToolContext, error, ok, register, short

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


def _tracked(ctx: ToolContext) -> dict:
    return ctx.state.setdefault("file_stamps", {})


def _stamp(path: Path):
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _remember(ctx: ToolContext, path: Path) -> None:
    try:
        _tracked(ctx)[str(path.resolve())] = _stamp(path)
    except OSError:
        pass


def _syntax_error(path: Path, text: str) -> str | None:
    ext = path.suffix.lower()
    try:
        if ext == ".py":
            compile(text, str(path), "exec")
        elif ext == ".json":
            json.loads(text)
        elif ext in (".yaml", ".yml"):
            import yaml
            list(yaml.safe_load_all(text))
        elif ext == ".toml":
            import tomllib
            tomllib.loads(text)
        elif ext in (".sh", ".bash") and shutil.which("bash"):
            r = subprocess.run(["bash", "-n"], input=text.encode(), capture_output=True, timeout=10)
            if r.returncode:
                return r.stderr.decode(errors="replace").strip()
    except SyntaxError as exc:
        return f"{exc.__class__.__name__}: {exc.msg} (line {exc.lineno})"
    except Exception as exc:  # json / yaml / toml errors
        return f"{exc.__class__.__name__}: {exc}"
    return None


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return False


def _suggest(path: Path) -> list[str]:
    parent = path.parent if path.parent.exists() else None
    if not parent:
        return []
    names = [p.name for p in parent.iterdir()][:500]
    return [str(parent / n) for n in difflib.get_close_matches(path.name, names, n=5, cutoff=0.5)]


# ── read_file ─────────────────────────────────────────────────────────────
def read_file(args: dict, ctx: ToolContext) -> str:
    raw = args.get("path")
    if not raw:
        return error("read_file: missing required field 'path'.")
    path = ctx.resolve(raw)
    if not path.exists():
        return error(f"File not found: {path}", similar=_suggest(path))
    if path.is_dir():
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))[:200]
        return error(f"{path} is a directory. Use search_files(target='files') to explore it.",
                     entries=[p.name + ("/" if p.is_dir() else "") for p in entries])
    if path.suffix.lower() == ".pdf" and shutil.which("pdftotext"):
        text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                              timeout=60).stdout.decode(errors="replace")
    elif _is_binary(path):
        return error(f"{path} looks like a binary file and cannot be read as text.")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    mode = str(ctx.cfg.get_path("agent.outline", "auto")).lower()
    if args.get("outline_only"):
        from .outline import outline
        ol = outline(path, text)
        return ok(path=str(path), total_lines=len(lines), outline=ol or "(no outline for this file type)",
                  note="Outline only. read_file with offset/limit to see the code (it counts as reading the file).")
    explicit_range = args.get("offset") not in (None, 1) or args.get("limit") is not None
    offset = max(1, int(args.get("offset") or 1))
    full_lines = int(ctx.cfg.get_path("agent.read_full_lines", 2000))
    default_limit = 2000 if (len(lines) <= full_lines or mode == "off") else int(ctx.cfg.get_path("agent.read_head_lines", 300))
    limit = min(2000, max(1, int(args.get("limit") or default_limit)))
    budget = int(ctx.cfg.get_path("agent.read_chars", 60000))
    key = (str(path.resolve()), offset, limit)
    cache = ctx.state.setdefault("read_cache", {})
    stamp = _stamp(path)
    call_id = args.get("_call_id")
    live = ctx.state.get("live_call_ids")  # tool results still in the conversation (set by the agent)

    def still_visible(entry) -> bool:
        # An earlier read only counts while its result is still in the conversation: after
        # compaction, /undo or /retry it is gone, and "unchanged" would leave the model blind.
        return isinstance(entry, tuple) and entry[0] == stamp and live is not None and entry[1] in live

    if still_visible(cache.get(key)):
        _remember(ctx, path)
        return ok(path=str(path), unchanged=True,
                  note="File unchanged since your last read_file of this range; its content is already in the conversation above.")
    out, used, end = [], 0, offset - 1
    for n in range(offset, min(len(lines), offset + limit - 1) + 1):
        row = f"{n}|{lines[n - 1]}"
        if used + len(row) > budget and out:
            break
        out.append(row)
        used += len(row) + 1
        end = n
    cache[key] = (stamp, call_id)
    _remember(ctx, path)
    _mark_seen(ctx, path, stamp, offset, end)
    result = {"path": str(path), "total_lines": len(lines)}
    # Outline first (classes, functions, parameters, types, line ranges) for longer files, once per
    # version of the file per session, so the model knows where everything is without reading it all.
    shown = ctx.state.setdefault("outline_shown", {})
    if mode != "off" and (mode == "always" or len(lines) > int(ctx.cfg.get_path("agent.outline_min_lines", 150))) \
            and not still_visible(shown.get(str(path.resolve()))) and (offset == 1 or end < len(lines)):
        from .outline import outline
        if ol := outline(path, text):
            result["outline"] = ol
            shown[str(path.resolve())] = (stamp, call_id)
    result["content"] = "\n".join(out)
    if end < len(lines):
        result["next_offset"] = end + 1
        if not explicit_range and limit < 2000:
            result["note"] = (f"Long file: showing lines {offset}-{end} of {len(lines)} plus the outline above. Read "
                              "the part you need with offset and limit (line ranges are in the outline).")
        else:
            result["note"] = f"Showing lines {offset}-{end} of {len(lines)}. Continue with offset={end + 1}."
    return ok(**result)


def _mark_seen(ctx: ToolContext, path: Path, stamp, first: int, last: int) -> None:
    """Remember which lines of this version of the file the model has actually seen."""
    seen = ctx.state.setdefault("seen_lines", {})
    rec = seen.get(str(path.resolve()))
    if not rec or rec[0] != stamp:
        rec = seen[str(path.resolve())] = (stamp, set())
    if last >= first:
        rec[1].update(range(first, last + 1))


def _unseen(ctx: ToolContext, path: Path, total: int) -> int:
    rec = ctx.state.get("seen_lines", {}).get(str(path.resolve()))
    if not rec or rec[0] != _stamp(path):
        return 0  # read through another route (patch, write); the stamp guard covers staleness
    return max(0, total - len(rec[1]))


# ── write_file ────────────────────────────────────────────────────────────
def write_file(args: dict, ctx: ToolContext) -> str:
    if not args.get("path") or not isinstance(args.get("path"), str):
        return error("write_file: missing required field 'path'. Re-emit the tool call with both 'path' and 'content' set.")
    if "content" not in args:
        return error("write_file: missing required field 'content'. The tool call included a path but no content "
                     "argument. Re-emit the tool call with the full content payload.")
    content = args["content"]
    if not isinstance(content, str):
        return error(f"write_file: 'content' must be a string, got {type(content).__name__}.")
    path = ctx.resolve(args["path"])
    before_err = None
    if path.exists():
        if path.is_dir():
            return error(f"{path} is a directory.")
        known = _tracked(ctx).get(str(path.resolve()))
        if known is None:
            return error(f"Refused (file untouched): {path} already exists and you have not read it in this task. "
                         "Call read_file first, merge your changes, then retry — or use patch for a targeted edit.")
        if known != _stamp(path):
            return error(f"Refused (file untouched): {path} changed on disk since you last read it. "
                         "read_file it again, merge, then retry.")
        old_text = path.read_text(encoding="utf-8", errors="replace")
        old_n = len(old_text.splitlines())
        unseen = _unseen(ctx, path, old_n)
        new_n = len(content.splitlines())
        if unseen > 20 and new_n < old_n * 0.8 and not args.get("overwrite_unseen"):
            return error(f"Refused (file untouched): you have only seen {old_n - unseen} of {path}'s {old_n} lines, "
                         f"and this write ({new_n} lines) would drop the rest. Use patch for the lines you want to "
                         "change, or read_file the remaining parts first. If replacing the whole file is really "
                         "intended, retry with overwrite_unseen=true.")
        before_err = _syntax_error(path, old_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    _remember(ctx, path)
    ctx.state.setdefault("read_cache", {}).clear()
    result = {"path": str(path), "bytes": len(content.encode()), "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1),
              "created": not existed, "verified": True}
    err = _syntax_error(path, content)
    if err and err != before_err:
        result["syntax_error"] = err
        result["note"] = "The file was written but has a syntax error — fix it."
    return ok(**result)


# ── patch ─────────────────────────────────────────────────────────────────
def _numbered(lines: list[str], first: int, last: int) -> str:
    a, b = max(1, first), min(len(lines), last)
    return "\n".join(f"{n}|{lines[n - 1].rstrip(chr(10))}" for n in range(a, b + 1))


def _patch_lines(args: dict, ctx: ToolContext, path: Path, original: str) -> tuple[str, str] | str:
    """Line-range mode: replace lines start_line..end_line (as shown by read_file) with new_string."""
    known = _tracked(ctx).get(str(path.resolve()))
    if known is None or known != _stamp(path):
        return error("Line numbers need a fresh view of the file: read_file it first (it changed since you last "
                     "read it, or you have not read it in this task), then retry with the new line numbers.")
    try:
        start, end = int(args["start_line"]), int(args.get("end_line", args["start_line"]))
    except (TypeError, ValueError):
        return error("start_line and end_line must be line numbers.")
    lines = original.splitlines(keepends=True)
    if start < 1 or start > len(lines) + 1 or end < start - 1 or end > len(lines):
        return error(f"Line range {start}-{end} is outside the file (it has {len(lines)} lines). To insert without "
                     "replacing anything, set end_line = start_line - 1.")
    new = str(args.get("new_string", ""))
    stripped = fuzzy._strip_line_numbers(new)
    note = ""
    if stripped is not None:
        new, note = stripped, "removed the 'N|' line-number prefixes from new_string"
    if new and not new.endswith("\n") and (end < len(lines) or original.endswith("\n")):
        new += "\n"
    updated = "".join(lines[:start - 1]) + new + "".join(lines[end:])
    label = f"lines {start}-{end}" if end >= start else f"inserted before line {start}"
    return updated, label + (f"; {note}" if note else "")


def patch(args: dict, ctx: ToolContext) -> str:
    line_mode = args.get("start_line") is not None
    required = ("path", "new_string") if line_mode else ("path", "old_string", "new_string")
    for field in required:
        if field not in args:
            return error(f"patch: missing required field '{field}'. Give path + old_string + new_string, or "
                         "path + start_line + end_line + new_string.")
    path = ctx.resolve(args["path"])
    if not path.is_file():
        return error(f"File not found: {path}", similar=_suggest(path))
    original = path.read_text(encoding="utf-8", errors="replace")
    attempts = ctx.state.setdefault("patch_failures", {})
    if line_mode:
        res = _patch_lines(args, ctx, path, original)
        if isinstance(res, str):
            return res
        updated, strategy = res
        count = 1
    else:
        try:
            updated, strategy, count = fuzzy.apply(original, str(args["old_string"]), str(args["new_string"]),
                                                   bool(args.get("replace_all", False)))
        except ValueError as exc:
            attempts[str(path)] = attempts.get(str(path), 0) + 1
            msg = str(exc)
            if attempts[str(path)] >= 2:
                msg += (" This file has failed to patch more than once: stop retrying old_string. Use start_line/"
                        "end_line with the line numbers from read_file. Do not rewrite the whole file.")
            return error(msg, path=str(path))
    attempts.pop(str(path), None)
    before_err = _syntax_error(path, original)
    path.write_text(updated, encoding="utf-8")
    _remember(ctx, path)
    ctx.state.setdefault("read_cache", {}).clear()
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), updated.splitlines(keepends=True),
                                        fromfile=str(path), tofile=str(path), n=2))
    if len(diff) > 6000:
        diff = diff[:6000] + "\n[diff truncated]"
    result = {"path": str(path), "replacements": count, "match": strategy, "diff": diff}
    # Where the edit landed, with the file's NEW line numbers, so the next edit lines up.
    new_lines = updated.splitlines()
    first_changed = next((i for i, (a, b) in enumerate(zip(original.splitlines(), new_lines)) if a != b),
                         min(len(original.splitlines()), len(new_lines)))
    span = abs(len(new_lines) - len(original.splitlines())) + max(1, len(str(args.get("new_string", "")).splitlines()))
    result["now"] = _numbered(new_lines, first_changed, first_changed + span + 1)  # 1 line of context before
    if len(new_lines) != len(original.splitlines()):
        result["note"] = (f"The file now has {len(new_lines)} lines ({len(new_lines) - len(original.splitlines()):+d}); "
                          "line numbers after this edit have shifted.")
    err = _syntax_error(path, updated)
    if err and err != before_err:
        result["syntax_error"] = err
        result["note"] = (result.get("note", "") + " The edit applied but introduced a syntax error — fix it.").strip()
    return ok(**result)


# ── search_files ──────────────────────────────────────────────────────────
def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            yield Path(dirpath) / f


def search_files(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not pattern:
        return error("search_files: missing required field 'pattern'.")
    root = ctx.resolve(args.get("path") or ".")
    if not root.exists():
        return error(f"Path not found: {root}")
    target = args.get("target", "content")
    limit, offset = int(args.get("limit") or 50), int(args.get("offset") or 0)
    rg = shutil.which("rg")
    if target == "files":
        if rg:
            r = subprocess.run([rg, "--files", "--iglob", pattern if any(c in pattern for c in "*?[") else f"*{pattern}*", str(root)],
                               capture_output=True, timeout=60)
            files = r.stdout.decode(errors="replace").splitlines()
        else:
            files = [str(p) for p in _walk(root) if fnmatch.fnmatch(p.name.lower(), pattern.lower())]
        if args.get("order") == "modified":
            files.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
        page = files[offset:offset + limit]
        res = {"total": len(files), "files": page}
        if offset + limit < len(files):
            res["next_offset"] = offset + limit
        return ok(**res)
    mode = args.get("output_mode", "content")
    context = int(args.get("context") or 0)
    if rg:
        cmd = [rg, "--no-heading", "--color", "never", "--max-columns", "400", "--max-columns-preview"]
        if mode == "files_only":
            cmd.append("-l")
        elif mode == "count":
            cmd.append("-c")
        else:
            cmd.append("-n")
            if context:
                cmd += ["-C", str(context)]
        if args.get("file_glob"):
            cmd += ["-g", args["file_glob"]]
        cmd += ["-e", pattern, str(root)]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 2 and not r.stdout:
            return error("ripgrep error: " + r.stderr.decode(errors="replace").strip())
        rows = r.stdout.decode(errors="replace").splitlines()
    else:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return error(f"Invalid regex: {exc}")
        rows, glob = [], args.get("file_glob")
        files = [root] if root.is_file() else _walk(root)
        for p in files:
            if glob and not fnmatch.fnmatch(p.name, glob):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits = [(i, l) for i, l in enumerate(text.splitlines(), 1) if rx.search(l)]
            if not hits:
                continue
            if mode == "files_only":
                rows.append(str(p))
            elif mode == "count":
                rows.append(f"{p}:{len(hits)}")
            else:
                rows += [f"{p}:{i}:{short(l, 400)}" for i, l in hits]
    page = rows[offset:offset + limit]
    res = {"total": len(rows), "matches": "\n".join(page) if page else "(no matches)"}
    if offset + limit < len(rows):
        res["next_offset"] = offset + limit
    return ok(**res)


register(Tool(
    name="read_file", emoji="📖", read_only=True,
    schema={"name": "read_file", "description": (
        "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. "
        "Output format: 'LINE_NUM|CONTENT'. Longer files start with an outline: every class, function and method "
        "with its parameters, types and line range (Lstart-end). Long files (over 400 lines) read without "
        "offset/limit return the outline plus the first lines: then read the part you need with offset and "
        "limit. outline_only=true returns just the outline (cheap; it does not count as reading the file). "
        "Suggests similar filenames if not found. PDFs are converted to text when pdftotext is installed. "
        "Cannot read images/binary."), "parameters": {
        "type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)"},
            "offset": {"type": "integer", "minimum": 1, "default": 1, "description": "Line number to start reading from (1-indexed, default: 1)"},
            "limit": {"type": "integer", "maximum": 2000, "description": "Maximum number of lines to read (max 2000)."},
            "outline_only": {"type": "boolean", "default": False, "description": "Return only the file's outline (classes, functions, signatures, line ranges)."},
        }, "required": ["path"]}},
    handler=read_file, label=lambda a: short(a.get("path", ""), 80) + ("  (outline)" if a.get("outline_only") else "") + (
        f"  @{a['offset']}" if a.get("offset", 1) not in (1, None) else ""),
))

register(Tool(
    name="write_file", emoji="✍",
    schema={"name": "write_file", "description": (
        "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in "
        "terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for "
        "targeted edits. For an EXISTING file, call read_file first: write_file refuses (file untouched) when "
        "this task has no current read/write of the file or the file changed on disk since; on refusal, "
        "read_file, merge, then retry. Auto-runs syntax checks on .py/.json/.yaml/.toml/.sh; only NEW errors "
        "introduced by this write are surfaced. The result's verified:true means the write landed — do NOT "
        "re-read the file to check it. Prefer several small, focused files over one huge file. To change part "
        "of an existing file, use patch, not a full rewrite."), "parameters": {
        "type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file to write (created if it doesn't exist, overwritten if it does)"},
            "content": {"type": "string", "description": "Complete content to write to the file"},
            "overwrite_unseen": {"type": "boolean", "default": False, "description": "Only when replacing a whole existing file whose lines you have not all read is intended."},
        }, "required": ["path", "content"]}},
    handler=write_file, label=lambda a: f"{short(a.get('path', ''), 70)}  ({len(str(a.get('content', '')).splitlines())} lines)",
))

register(Tool(
    name="patch", emoji="🩹",
    schema={"name": "patch", "description": (
        "Targeted edits in files. Use this instead of sed/awk in terminal. Two ways:\n"
        "1. old_string + new_string: find a unique string and replace it. Fuzzy matching tolerates whitespace, "
        "indentation and quoting differences.\n"
        "2. start_line + end_line + new_string: replace those lines (numbers exactly as read_file shows them) "
        "with new_string. Best for code with quotes or backslashes, or after old_string failed. Insert without "
        "replacing: end_line = start_line - 1. Requires a read_file of the current file first.\n"
        "Returns a diff and the edited region with its new line numbers. Auto-runs syntax checks. Never rewrite a "
        "large file to make a small change."), "parameters": {
        "type": "object", "properties": {
            "path": {"type": "string", "description": "File path to edit."},
            "old_string": {"type": "string", "description": "Exact text to find and replace (way 1). Must be unique unless replace_all=true. Do not include the N| line-number prefixes from read_file."},
            "new_string": {"type": "string", "description": "Replacement text. Pass '' to delete."},
            "start_line": {"type": "integer", "description": "Way 2: first line to replace (1-indexed, from read_file)."},
            "end_line": {"type": "integer", "description": "Way 2: last line to replace, inclusive."},
            "replace_all": {"type": "boolean", "default": False, "description": "Way 1 only: replace every occurrence."},
        }, "required": ["path", "new_string"]}},
    handler=patch, label=lambda a: short(a.get("path", ""), 80) + (
        f"  lines {a.get('start_line')}-{a.get('end_line', a.get('start_line'))}" if a.get("start_line") is not None else ""),
))

register(Tool(
    name="search_files", emoji="🔎", read_only=True,
    schema={"name": "search_files", "description": (
        "Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. "
        "Ripgrep-backed, faster than shell equivalents.\n\nContent search (target='content'): Regex search inside "
        "files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search "
        "(target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls."),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search"},
            "target": {"type": "string", "enum": ["content", "files"], "default": "content", "description": "'content' searches inside file contents, 'files' searches for files by name"},
            "path": {"type": "string", "default": ".", "description": "Directory or file to search in (default: current working directory)"},
            "file_glob": {"type": "string", "description": "Filter files by pattern in content mode (e.g., '*.py')"},
            "limit": {"type": "integer", "default": 50, "description": "Maximum number of results to return (default: 50)"},
            "offset": {"type": "integer", "default": 0, "description": "Skip first N results for pagination (default: 0)"},
            "order": {"type": "string", "enum": ["discovery", "modified"], "default": "discovery", "description": "File-search order; 'modified' = newest first"},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "default": "content", "description": "Output format for content mode"},
            "context": {"type": "integer", "default": 0, "description": "Lines of context before and after each match (content mode only)"},
        }, "required": ["pattern"]}},
    handler=search_files, label=lambda a: f"{a.get('pattern', '')}  in {short(a.get('path', '.'), 50)}",
))
