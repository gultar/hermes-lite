"""File outlines: every class, function and method with its parameters, types and line range.

Python is parsed exactly (ast). Brace languages (JS/TS, Go, Rust, Java, C-family, PHP) are scanned
with patterns, and each block's end line comes from brace matching that skips strings and
comments. Markdown, JSON, YAML and TOML get their headings or top-level keys. Line numbers match
read_file's, so the model can jump with offset or edit with patch start_line/end_line.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

MAX_ENTRIES = 160
MAX_CHARS = 7000

JS_EXT = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"}
EMBED_EXT = {".vue", ".svelte", ".html", ".htm", ".astro"}
BRACE_EXT = {".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".cs": "csharp", ".c": "c",
             ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".php": "php", ".swift": "swift",
             ".dart": "dart", ".scala": "scala"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "else", "do", "try", "with", "new",
            "typeof", "await", "yield", "super", "constructor_", "elif", "foreach", "using", "lock", "sizeof"}


# ── Python ──────────────────────────────────────────────────────────────
def _py_args(a: ast.arguments) -> str:
    def one(arg: ast.arg, default=None) -> str:
        s = arg.arg + (f": {ast.unparse(arg.annotation)}" if arg.annotation else "")
        if default is not None:
            s += ("=" if not arg.annotation else " = ") + _short(ast.unparse(default), 30)
        return s
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    parts = [one(x, d) for x, d in zip(pos, defaults)]
    if a.posonlyargs:
        parts.insert(len(a.posonlyargs), "/")
    if a.vararg:
        parts.append("*" + one(a.vararg))
    elif a.kwonlyargs:
        parts.append("*")
    parts += [one(x, d) for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        parts.append("**" + one(a.kwarg))
    return ", ".join(parts)


def _short(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _py_outline(text: str) -> list[tuple[int, int | None, int, str]]:
    tree = ast.parse(text)
    out: list[tuple[int, int | None, int, str]] = []

    def deco(node) -> str:
        names = [_short(ast.unparse(d), 30) for d in getattr(node, "decorator_list", [])]
        return " ".join("@" + n for n in names) + (" " if names else "")

    def walk(body, depth: int):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                ret = f" -> {_short(ast.unparse(node.returns), 40)}" if node.returns else ""
                out.append((node.lineno, node.end_lineno, depth, f"{deco(node)}{kw} {node.name}({_py_args(node.args)}){ret}"))
            elif isinstance(node, ast.ClassDef):
                bases = ", ".join(_short(ast.unparse(b), 30) for b in node.bases + node.keywords)
                out.append((node.lineno, node.end_lineno, depth, f"{deco(node)}class {node.name}" + (f"({bases})" if bases else "")))
                walk(node.body, depth + 1)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                val = f" = {_short(ast.unparse(node.value), 40)}" if node.value is not None else ""
                out.append((node.lineno, None, depth, f"{node.target.id}: {_short(ast.unparse(node.annotation), 40)}{val}"))
            elif isinstance(node, ast.Assign) and depth == 0 and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.isupper() or name.startswith("__"):
                    out.append((node.lineno, None, depth, f"{name} = {_short(ast.unparse(node.value), 50)}"))
            elif isinstance(node, (ast.If, ast.Try)) and depth == 0:
                walk(node.body, depth)  # e.g. `if TYPE_CHECKING:` or try/except imports
    walk(tree.body, 0)
    return out


def _py_fallback(text: str) -> list[tuple[int, int | None, int, str]]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(\s*)((?:async\s+)?def\s+\w+\s*\(.*|class\s+\w+.*?):?\s*$", line)
        if m:
            out.append((i, None, len(m.group(1)) // 4, m.group(2).rstrip(":")))
    return out


# ── brace languages ─────────────────────────────────────────────────────
def _block_end(lines: list[str], start: int) -> int | None:
    """1-based end line of the {...} block opening on/after line *start*, skipping strings/comments.
    Braces inside parentheses (destructured or default-object parameters) are not the body."""
    depth, opened, parens = 0, False, 0
    in_str: str | None = None
    in_block_comment = False
    for li in range(start - 1, min(len(lines), start - 1 + 5000)):
        line, j = lines[li], 0
        while j < len(line):
            ch, nxt = line[j], line[j + 1] if j + 1 < len(line) else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment, j = False, j + 1
            elif in_str:
                if ch == "\\":
                    j += 1
                elif ch == in_str:
                    in_str = None
            elif ch == "/" and nxt == "/":
                break
            elif ch == "/" and nxt == "*":
                in_block_comment, j = True, j + 1
            elif ch in "\"'`":
                in_str = ch
            elif ch == "(":
                parens += 1
            elif ch == ")":
                parens = max(0, parens - 1)
            elif ch == "{" and (parens == 0 or opened):
                depth, opened = depth + 1, True
            elif ch == "}" and (parens == 0 or opened):
                depth -= 1
                if opened and depth == 0:
                    return li + 1
            j += 1
        if in_str in ("'", '"'):
            in_str = None  # unterminated quote: do not let it swallow the file
        if not opened and li > start + 3:
            return None
    return None


def _join_signature(lines: list[str], i: int) -> str:
    """Join a signature that continues over several lines, until parentheses balance."""
    sig = lines[i].rstrip()
    k = i
    while sig.count("(") > sig.count(")") and k + 1 < len(lines) and k - i < 12:
        k += 1
        sig += " " + lines[k].strip()
    return sig


JS_PATTERNS = [
    (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?class\s+([\w$]+)([^{]*)"), "class"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:declare\s+)?interface\s+([\w$]+)([^{]*)"), "interface"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:declare\s+)?type\s+([\w$]+)\s*(<[^=]*>)?\s*=\s*(.*)"), "type"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([\w$]+)"), "enum"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:declare\s+)?(async\s+)?function\s*\*?\s*([\w$]+)\s*(<[^>(]*>)?\s*\((.*)"), "function"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::\s*[^=]+)?=\s*(async\s+)?(?:function\b[^(]*)?\((.*)"), "arrow"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::\s*[^=]+)?=\s*(async\s+)?([\w$]+)\s*=>"), "arrow1"),
    (re.compile(r"^(\s*)((?:[\w$]+\.)+[\w$]+)\s*=\s*(async\s+)?function\b\s*\*?\s*[\w$]*\s*\((.*)"), "member"),
    (re.compile(r"^(\s*)((?:[\w$]+\.)+[\w$]+)\s*=\s*(async\s+)?\((.*)"), "member_arrow"),
]
JS_METHOD = re.compile(r"^(\s+)(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*"
                       r"\*?\s*(#?[\w$]+)\s*(<[^>(]*>)?\s*\((.*)")
TS_MEMBER = re.compile(r"^(\s+)(?:readonly\s+)?([\w$]+)(\?)?\s*(\(.*|:\s*.+)")
JS_TOPVAR = re.compile(r"^(?:export\s+)?const\s+([\w$]+)\s*(:\s*[^=]+)?=\s*(.{0,60})")


def _params(sig_after_paren: str) -> tuple[str, str]:
    """('a, b = 1', ': ReturnType') from the text after the opening parenthesis."""
    depth, j = 1, 0
    while j < len(sig_after_paren) and depth:
        if sig_after_paren[j] == "(":
            depth += 1
        elif sig_after_paren[j] == ")":
            depth -= 1
        j += 1
    params = sig_after_paren[: j - 1] if depth == 0 else sig_after_paren
    rest = sig_after_paren[j:] if depth == 0 else ""
    ret = re.match(r"\s*:\s*([^{=;]+?)\s*(?:=>|\{|;|$)", rest)
    return _short(params, 120), (f": {_short(ret.group(1), 40)}" if ret else "")


def _js_outline(text: str, line_offset: int = 0) -> list[tuple[int, int | None, int, str]]:
    lines = text.splitlines()
    out: list[tuple[int, int | None, int, str]] = []
    blocks: list[tuple[int, str, int]] = []  # open (end line, kind, indent) — for nesting depth

    def add(n: int, end: int | None, label: str) -> None:
        out.append((n, end, min(len(blocks), 3), label))

    for i, raw in enumerate(lines):
        n = i + 1
        while blocks and n > blocks[-1][0]:
            blocks.pop()
        stripped = raw.strip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        inside = blocks[-1][1] if blocks else None
        if len(blocks) >= 3:
            continue  # too deep to be worth listing
        sig = _join_signature(lines, i) if "(" in raw else raw.rstrip()
        indent = len(raw) - len(raw.lstrip())
        matched = False
        for rx, kind in JS_PATTERNS:
            m = rx.match(sig)
            if not m:
                continue
            if kind in ("class", "interface"):
                end = _block_end(lines, n)
                add(n, end, f"{kind} {m.group(2)}{(' ' + _short(m.group(3), 60).strip()) if m.group(3).strip() else ''}")
                if end:
                    blocks.append((end, kind, indent))
            elif kind == "type":
                add(n, None, f"type {m.group(2)}{m.group(3) or ''} = {_short(m.group(4), 70)}")
            elif kind == "enum":
                add(n, _block_end(lines, n), f"enum {m.group(2)}")
            elif kind == "function":
                params, ret = _params(m.group(5))
                end = None if sig.rstrip().endswith(";") else _block_end(lines, n)
                add(n, end, f"{m.group(2) or ''}function {m.group(3)}({params}){ret}")
                if end:
                    blocks.append((end, "function", indent))
            elif kind == "arrow":
                if "=>" not in sig and "function" not in sig:
                    continue
                params, ret = _params(m.group(4))
                end = _block_end(lines, n) if "{" in sig.split("=>", 1)[-1] or "function" in sig else None
                add(n, end, f"const {m.group(2)} = {m.group(3) or ''}({params}){ret} =>")
                if end:
                    blocks.append((end, "function", indent))
            elif kind == "arrow1":
                add(n, _block_end(lines, n) if "{" in sig.split("=>", 1)[-1] else None,
                    f"const {m.group(2)} = {m.group(3) or ''}{m.group(4)} =>")
            elif kind in ("member", "member_arrow"):
                if kind == "member_arrow" and "=>" not in sig:
                    continue
                params, ret = _params(m.group(4))
                end = _block_end(lines, n)
                add(n, end, f"{m.group(2)} = {m.group(3) or ''}function({params}){ret}" if kind == "member"
                    else f"{m.group(2)} = {m.group(3) or ''}({params}){ret} =>")
                if end:
                    blocks.append((end, "function", indent))
            matched = True
            break
        if matched:
            continue
        if inside == "class":
            m = JS_METHOD.match(sig)
            if m and m.group(2) not in KEYWORDS and indent > blocks[-1][2]:
                params, ret = _params(m.group(4))
                head = sig.split("(")[0]
                prefix = " ".join(w for w in ("static", "async", "get", "set") if re.search(rf"\b{w}\s", head))
                end = _block_end(lines, n)
                if end:  # a bare call such as `foo(x);` in a field initializer has no block
                    add(n, end, f"{prefix + ' ' if prefix else ''}{m.group(2)}({params}){ret}")
                    blocks.append((end, "method", indent))
                    continue
        if inside == "interface":
            m = TS_MEMBER.match(raw)
            if m and indent > blocks[-1][2]:
                if m.group(4).startswith("("):
                    params, ret = _params(m.group(4)[1:])
                    add(n, None, f"{m.group(2)}{m.group(3) or ''}({params}){ret}")
                else:
                    add(n, None, f"{m.group(2)}{m.group(3) or ''}{_short(m.group(4).rstrip(';,'), 70)}")
                continue
        if not blocks and indent == 0:
            m = JS_TOPVAR.match(raw)
            if m and "=>" not in raw and "function" not in raw:
                typ = m.group(2).strip() if m.group(2) else ""
                add(n, None, f"const {m.group(1)}{typ} = {_short(m.group(3), 50)}")
    return [(a + line_offset, (b + line_offset) if b else None, d, s_) for a, b, d, s_ in out]


BRACE_PATTERNS = {
    "go": [re.compile(r"^func\s+(\([^)]*\)\s*)?(\w+)\s*(\[[^\]]*\])?\s*\((.*)"), re.compile(r"^type\s+(\w+)\s+(struct|interface)\b")],
    "rust": [re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)\s*(<[^(]*>)?\s*\((.*)"),
             re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|impl|mod)\b\s*([^{;]*)")],
}
GENERIC_FN = re.compile(r"^(\s*)(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async|"
                        r"inline|extern|const|unsafe|suspend|open|func|fun|function|def)\s+)*"
                        r"(?:[\w<>\[\],.*&:?]+\s+)+?([A-Za-z_]\w*)\s*\((.*)")
GENERIC_TYPE = re.compile(r"^(\s*)(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|data|export)\s+)*"
                          r"(class|struct|interface|enum|trait|record|object|namespace)\s+([A-Za-z_]\w*)")


def _brace_outline(text: str, lang: str) -> list[tuple[int, int | None, int, str]]:
    lines = text.splitlines()
    out = []
    for i, raw in enumerate(lines):
        n = i + 1
        s = raw.strip()
        if not s or s.startswith(("//", "/*", "*", "#")) or s.endswith(";") and "{" not in s:
            continue
        sig = _join_signature(lines, i) if "(" in raw else raw
        depth = (len(raw) - len(raw.lstrip())) // 4
        if lang in BRACE_PATTERNS:
            for rx in BRACE_PATTERNS[lang]:
                m = rx.match(sig)
                if m:
                    out.append((n, _block_end(lines, n), min(depth, 2), _short(sig.split("{")[0].strip(), 140)))
                    break
            continue
        m = GENERIC_TYPE.match(sig)
        if m:
            out.append((n, _block_end(lines, n), min(depth, 2), _short(sig.split("{")[0].strip(), 120)))
            continue
        m = GENERIC_FN.match(sig)
        if m and m.group(2) not in KEYWORDS and "=" not in sig.split("(")[0]:
            end = _block_end(lines, n)
            if end:
                out.append((n, end, min(depth, 2), _short(sig.split("{")[0].strip(), 140)))
    return out


# ── other formats ───────────────────────────────────────────────────────
def _embedded_scripts(text: str) -> list[tuple[int, int | None, int, str]]:
    out = []
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.S | re.I):
        start_line = text.count("\n", 0, m.start(1))
        out += _js_outline(m.group(1), line_offset=start_line)
    for m in re.finditer(r'\bid="([^"]+)"', text):
        if len(out) > MAX_ENTRIES:
            break
        out.append((text.count("\n", 0, m.start()) + 1, None, 0, f'#{m.group(1)}'))
    return sorted(out)


def _markdown(text: str):
    out, fence = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            out.append((i, None, len(m.group(1)) - 1, m.group(2).strip()))
    return out


def _keys(text: str, ext: str):
    lines = text.splitlines()
    out = []
    if ext == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return []
        if isinstance(data, dict):
            for k, v in data.items():
                line = next((i for i, l in enumerate(lines, 1) if re.match(rf'^\s{{0,4}}"{re.escape(str(k))}"\s*:', l)), None)
                kind = type(v).__name__.replace("dict", "object").replace("list", f"array[{len(v)}]" if isinstance(v, list) else "list")
                out.append((line or 1, None, 0, f"{k}: {kind}"))
        return out
    for i, line in enumerate(lines, 1):
        if ext in (".yaml", ".yml") and re.match(r"^[A-Za-z_][\w.-]*\s*:", line):
            out.append((i, None, 0, line.split(":")[0].strip()))
        elif ext == ".toml" and re.match(r"^\[[^\]]+\]", line):
            out.append((i, None, 0, line.strip()))
    return out


# ── entry point ─────────────────────────────────────────────────────────
def outline(path: Path, text: str) -> str:
    ext = path.suffix.lower()
    try:
        if ext in (".py", ".pyi"):
            try:
                entries = _py_outline(text)
            except SyntaxError:
                entries = _py_fallback(text)
        elif ext in JS_EXT:
            entries = _js_outline(text)
        elif ext in EMBED_EXT:
            entries = _embedded_scripts(text)
        elif ext in BRACE_EXT:
            entries = _brace_outline(text, BRACE_EXT[ext])
        elif ext in (".md", ".markdown", ".mdx", ".rst"):
            entries = _markdown(text)
        elif ext in (".json", ".yaml", ".yml", ".toml"):
            entries = _keys(text, ext)
        elif ext in (".sh", ".bash", ".zsh", ".fish"):
            entries = [(i, None, 0, m.group(1)) for i, l in enumerate(text.splitlines(), 1)
                       if (m := re.match(r"^\s*(?:function\s+)?([\w-]+)\s*\(\)\s*\{?|^\s*function\s+([\w-]+)", l)) and m.group(1)]
        elif ext in (".rb", ".lua", ".ex", ".exs"):
            entries = [(i, None, (len(l) - len(l.lstrip())) // 2, l.strip())
                       for i, l in enumerate(text.splitlines(), 1)
                       if re.match(r"^\s*(def|class|module|function|local function|defp?|defmodule)\s", l)]
        else:
            return ""
    except RecursionError:
        return ""
    if not entries:
        return ""
    lines = []
    for start, end, depth, sig in entries[:MAX_ENTRIES]:
        where = f"L{start}-{end}" if end and end != start else f"L{start}"
        lines.append(f"{'  ' * depth}{where:<10} {sig}")
    more = len(entries) - MAX_ENTRIES
    text_out = "\n".join(lines) + (f"\n… {more} more entries" if more > 0 else "")
    if len(text_out) > MAX_CHARS:
        text_out = text_out[:MAX_CHARS].rsplit("\n", 1)[0] + "\n… (outline cut)"
    return text_out
