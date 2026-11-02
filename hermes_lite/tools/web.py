"""web_search (DuckDuckGo HTML or SearXNG — no API keys) and web_extract (page → markdown-ish text)."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from .base import Tool, ToolContext, error, ok, register, short, truncate_middle

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")


# ── search ────────────────────────────────────────────────────────────────
class _DDG(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._field = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            href = a.get("href") or ""
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            self.results.append({"title": "", "url": href, "description": ""})
            self._field = "title"
        elif tag in ("a", "div") and "result__snippet" in cls and self.results:
            self._field = "description"

    def handle_endtag(self, tag):
        if tag in ("a", "div"):
            self._field = None

    def handle_data(self, data):
        if self._field and self.results:
            self.results[-1][self._field] += data


def _ddg(query: str, limit: int) -> list[dict]:
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": UA},
                   timeout=20, follow_redirects=True)
    r.raise_for_status()
    p = _DDG()
    p.feed(r.text)
    out = [{k: " ".join(v.split()) for k, v in res.items()} for res in p.results
           if res["url"].startswith("http") and "duckduckgo.com/y.js" not in res["url"]]
    return out[:limit]


def _searxng(base: str, query: str, limit: int) -> list[dict]:
    r = httpx.get(base.rstrip("/") + "/search", params={"q": query, "format": "json"}, timeout=20)
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "description": short(x.get("content", ""), 300)}
            for x in r.json().get("results", [])[:limit]]


def web_search(args: dict, ctx: ToolContext) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return error("web_search: missing 'query'.")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    backend = ctx.cfg.get_path("web.search_backend", "duckduckgo")
    try:
        if backend == "searxng":
            results = _searxng(ctx.cfg.get_path("web.searxng_url"), query, limit)
        else:
            results = _ddg(query, limit)
    except httpx.HTTPError as exc:
        return error(f"Search failed ({backend}): {exc}")
    if not results:
        return ok(query=query, results=[], note="No results. Try different or broader terms.")
    return ok(query=query, results=results)


# ── extract ───────────────────────────────────────────────────────────────
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe", "template"}
    BLOCK = {"p", "div", "section", "article", "main", "br", "tr", "table", "ul", "ol", "blockquote", "figure"}

    def __init__(self, base: str):
        super().__init__()
        self.base, self.out, self.skip, self.pre, self.href = base, [], 0, 0, None
        self.title, self._in_title = "", False
        self.links: list[tuple[str, str, bool]] = []   # (text, url, in navigation/header/footer)
        self.rel: dict[str, str] = {}                   # rel=next / rel=prev
        self._link: list | None = None
        self.scripts = 0

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def _collect(self, tag, a):
        if tag == "script":
            self.scripts += 1
        rel = (a.get("rel") or "").lower()
        if tag in ("a", "link") and a.get("href") and not a["href"].startswith(("#", "javascript:", "mailto:")):
            url = urljoin(self.base, a["href"]).split("#")[0]
            for r in ("next", "prev", "previous"):
                if r in rel.split():
                    self.rel.setdefault("prev" if r == "previous" else r, url)
            if tag == "a":
                self._link = [url, [], self.skip > 0]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._collect(tag, a)
        if tag in self.SKIP:
            self.skip += 1
        if self.skip:
            return
        if tag == "title":
            self._in_title = True
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "a" and a.get("href") and not a["href"].startswith(("#", "javascript:")):
            self.href = urljoin(self.base, a["href"])
            self.out.append("[")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[IMAGE: {a['alt']}]")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            url, words, nav = self._link
            self.links.append((" ".join("".join(words).split())[:120], url, nav))
            self._link = None
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "a" and self.href:
            self.out.append(f"]({self.href})")
            self.href = None
        elif tag in self.BLOCK or re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n")

    def handle_data(self, data):
        if self._link is not None:
            self._link[1].append(data)
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        t = unescape("".join(self.out))
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


NEXT = re.compile(r"^\s*(next|suivant|siguiente|continue|weiter|forward)\b|(→|»|›)\s*$", re.I)
PREV = re.compile(r"^\s*(prev|previous|précédent|precedent|anterior|back|zurück)\b|^\s*(←|«|‹)", re.I)


def lightpanda(cfg) -> str | None:
    """The Lightpanda binary: web.lightpanda (a path), else Hermes Lite's own venv, else PATH."""
    import sys
    from pathlib import Path
    setting = str(cfg.get_path("web.lightpanda", "auto") or "auto")
    if setting in ("off", "none", "false"):
        return None
    if setting != "auto":
        return setting if Path(setting).expanduser().is_file() else None
    beside = Path(sys.executable).parent / ("lightpanda.exe" if sys.platform == "win32" else "lightpanda")
    return str(beside) if beside.is_file() else shutil.which("lightpanda")


def _render(url: str, cfg=None) -> tuple[str, str] | None:
    """(the page's HTML after its JavaScript ran, which browser ran it). Lightpanda first — a headless
    browser made for this: ~30 MB and a fraction of a second, where Chromium takes hundreds of MB
    (which can push parts of the model out of RAM) — then Chromium with a throwaway profile."""
    import os
    from .vision import _browsers, _run
    lp = lightpanda(cfg) if cfg is not None else None
    if lp:
        r = _run([lp, "fetch", "--dump", "html", "--strip-mode", "js", "--strip-mode", "css",
                  "--wait-until", "networkalmostidle", "--wait-ms", "8000", "--terminate-ms", "15000",
                  "--log-level", "error", url], 30)
        if r.returncode == 0 and "<" in (r.stdout or "") and len(r.stdout) > 200:
            return r.stdout, "Lightpanda"
    root = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    for exe, path in _browsers():
        with tempfile.TemporaryDirectory() as prof:
            head = [] if exe == "chrome-headless-shell" else ["--headless=new", "--no-first-run"]
            r = _run([path, *head, "--disable-gpu", *root, f"--user-data-dir={prof}", "--virtual-time-budget=6000",
                      "--dump-dom", url], 45)
        if r.returncode == 0 and "<" in (r.stdout or ""):
            return r.stdout, exe
    return None


def _links(p: "_Text", page_url: str, mode: str) -> dict:
    """next / previous page, plus the page's links: 'nav' = the navigation links (menus, sidebars,
    footers: where tutorials keep their chapter list); 'all' = every link, same site first."""
    out: dict = {}
    nxt = p.rel.get("next") or next((u for t, u, _ in p.links if t and NEXT.search(t) and u != page_url), None)
    prv = p.rel.get("prev") or next((u for t, u, _ in p.links if t and PREV.search(t) and u != page_url), None)
    if nxt:
        out["next"] = nxt
    if prv:
        out["previous"] = prv
    if mode in ("nav", "all"):
        host = urlparse(page_url).netloc
        seen, rows = set(), []
        for text, url, nav in p.links:
            if url in seen or url == page_url or not url.startswith("http") or (mode == "nav" and not nav):
                continue
            seen.add(url)
            rows.append((urlparse(url).netloc != host, f"{text or '(no text)'} — {url}"))
        rows = [r for _, r in sorted(rows, key=lambda x: x[0])]  # stable: same site first, page order kept
        cap = 30 if mode == "nav" else 200
        if rows:
            out["links" if mode == "all" else "navigation"] = rows[:cap] + ([f"… {len(rows) - cap} more"] if len(rows) > cap else [])
    return out


def _extract_one(url: str, ctx: ToolContext, limit: int, links: str = "nav", render: str = "auto",
                 links_only: bool = False) -> dict:
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        if render == "never":
            return {"url": url, "error": str(exc)}
        got = _render(url, ctx.cfg)  # some sites refuse plain HTTP clients but serve a browser
        if got is None:
            return {"url": url, "error": str(exc)}
        return _from_html(got[0], url, ctx, limit, links, links_only, rendered=got[1])
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        if not shutil.which("pdftotext"):
            return {"url": url, "error": "PDF needs pdftotext (pacman -S poppler)."}
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
            fh.write(r.content)
            fh.flush()
            text = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True).stdout.decode(errors="replace")
        title = url
    elif "html" in ctype or "<html" in r.text[:500].lower():
        if render == "always":
            got = _render(str(r.url), ctx.cfg)
            if got:
                return _from_html(got[0], str(r.url), ctx, limit, links, links_only, rendered=got[1])
        res = _from_html(r.text, str(r.url), ctx, limit, links, links_only)
        # Next to nothing but scripts: the page is built by JavaScript. Run it in a headless browser.
        if render == "auto" and res.pop("_thin", False):
            got = _render(str(r.url), ctx.cfg)
            if got:
                rendered = _from_html(got[0], str(r.url), ctx, limit, links, links_only, rendered=got[1])
                if len(rendered.get("content") or "") > len(res.get("content") or ""):
                    return rendered
        res.pop("_thin", None)
        return res
    else:
        text, title = r.text, url
    shown, more = _page_window(text, limit, ctx)
    return {"url": str(r.url), "title": title, "content": shown, **more}


def _from_html(html: str, url: str, ctx: ToolContext, limit: int, links: str, links_only: bool,
               rendered: str = "") -> dict:
    p = _Text(url)
    p.feed(html)
    text, title = p.text(), " ".join(p.title.split())
    if links_only:
        res = {"url": url, "title": title, **_links(p, url, "all")}
        res.pop("_thin", None)
        return res
    shown, more = _page_window(text, limit, ctx)
    res = {"url": url, "title": title, "content": shown, **more, **_links(p, url, links)}
    if rendered:
        res["rendered"] = f"JavaScript ran in a headless browser ({rendered})"
    elif len(text) < 400 and p.scripts >= 3:
        res["_thin"] = True
    return res


def _page_window(text: str, limit: int, ctx: ToolContext) -> tuple[str, dict]:
    """A page longer than the budget: its beginning (cut at a paragraph), and the whole text in a
    file the model continues reading with read_file from the given line."""
    if len(text) <= limit:
        return text, {}
    import time
    cut = text.rfind("\n\n", int(limit * 0.8), limit)
    cut = cut if cut > 0 else limit
    d = ctx.home / "scratch" / ctx.session_id / "web"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"page-{int(time.time() * 1000)}.txt"
    path.write_text(text, encoding="utf-8", errors="replace")
    line = text.count("\n", 0, cut) + 2  # the first line not shown
    return text[:cut], {"full_text_file": str(path), "total_chars": len(text), "shown_chars": cut,
                        "continue": f"read_file {path} with offset={line} for the rest ({len(text) - cut:,} more characters)"}


def _url_items(urls) -> list[tuple[str, dict]]:
    """'urls' as the model wrote it: a URL, a list of URLs, or objects like {"url": …, "links_only": true}
    (their own options apply to that URL)."""
    if isinstance(urls, (str, dict)):
        urls = [urls]
    out = []
    for item in urls if isinstance(urls, list) else []:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), {}))
        elif isinstance(item, dict):
            url = item.get("url") or item.get("href") or item.get("link")
            if isinstance(url, str) and url.strip():
                out.append((url.strip(), {k: v for k, v in item.items() if k in ("links_only", "links", "render", "char_limit")}))
    return out


def _urls_label(a: dict) -> str:
    return short(", ".join(u for u, _ in _url_items(a.get("urls"))), 90)


def web_extract(args: dict, ctx: ToolContext) -> str:
    items = _url_items(args.get("urls"))
    if not items:
        return error("web_extract: give 'urls', a list of up to 5 URLs as strings, e.g. "
                     "{\"urls\": [\"https://example.com\"], \"links_only\": true}.")
    items = items[:5]
    urls = [u for u, _ in items]
    per_page = int(ctx.cfg.get_path("web.extract_chars", 40000))
    total = int(ctx.cfg.get_path("web.extract_total_chars", 60000))
    asked = int(args.get("char_limit") or 0)
    # one page gets the full budget; several share the call's total, so a 5-URL call can't flood the context
    limit = max(2000, min(asked or per_page, total // len(urls)))
    results = []
    for url, own in items:
        opt = {**args, **own}
        page_limit = max(2000, min(int(own["char_limit"]), total // len(urls))) if own.get("char_limit") else limit
        results.append(_extract_one(url, ctx, page_limit, links=str(opt.get("links") or "nav").lower(),
                                    render=str(opt.get("render") or "auto").lower(),
                                    links_only=bool(opt.get("links_only"))))
    return ok(results=results)


register(Tool(
    name="web_search", emoji="🌐", read_only=True,
    schema={"name": "web_search", "description": (
        "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. "
        "Operators such as site:domain and \"exact phrase\" usually work."), "parameters": {
        "type": "object", "properties": {
            "query": {"type": "string", "description": "The search query to look up on the web."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "Maximum number of results (default 5)."},
        }, "required": ["query"]}},
    handler=web_search, label=lambda a: a.get("query", ""),
))

register(Tool(
    name="web_extract", emoji="📄", read_only=True,
    schema={"name": "web_extract", "description": (
        "Extract content from web page URLs. Returns clean page content in markdown/text (fast, no "
        "summarization). Also works with PDF URLs. Pages within the character budget return whole; a longer "
        "page returns its beginning plus the path of the full text and the read_file offset to continue from. "
        "Each page also gives 'next'/'previous' (the following/preceding page of a tutorial or docs) and its "
        "navigation links. links_only=true returns just the page's links, in order: use it on a tutorial's "
        "contents page, put the chapters in todo_list, then read and carry out one chapter at a time. Pages "
        "built by JavaScript are rendered in a headless browser automatically."), "parameters": {
        "type": "object", "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "maxItems": 5, "description": "URLs to extract (max 5 per call)"},
            "char_limit": {"type": "integer", "minimum": 2000, "description": "Per-page character budget (default: the user's setting, ~40000; several URLs share the call's total)."},
            "links_only": {"type": "boolean", "description": "Only the page's links (a site's or tutorial's table of contents), no content."},
            "links": {"type": "string", "enum": ["nav", "all", "none"], "description": "Links to list besides the content: nav (default: menus, sidebars, footers), all, none."},
            "render": {"type": "string", "enum": ["auto", "always", "never"], "description": "Run the page's JavaScript in a headless browser: auto (default: when the page is empty without it), always, never."},
        }, "required": ["urls"]}},
    handler=web_extract, label=_urls_label,
))
