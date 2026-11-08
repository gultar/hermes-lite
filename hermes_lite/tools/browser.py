"""browser: a page that stays open between calls, to click, type, choose, scroll and go back.

Lightpanda first (a headless browser made for agents: tens of MB, starts in a fraction of a second);
Chromium with a throwaway profile when Lightpanda cannot handle a page. Both are driven through
Playwright (the Python counterpart of Puppeteer), over the Chrome DevTools Protocol.

Every result is a snapshot of the page as text, with each link, button and field numbered:
the model acts on the numbers ([12] → browser(action="click", ref=12)). Never the user's own browser
or logins; a password is never typed; submitting a form or clicking anything that buys, sends,
deletes or signs up asks the user first (unless approvals are off)."""

from __future__ import annotations

import atexit
import json
import importlib.util
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from .base import Tool, ToolContext, error, ok, register, short

SNAPSHOT_CHARS = 8000          # per result (~2.5k tokens); the rest: action="read" with an offset
IDLE_MINUTES = 10              # an idle browser is closed (Chromium holds hundreds of MB of RAM)
RISKY = re.compile(r"\b(buy|pay|checkout|check out|order|purchase|place order|subscribe|sign ?up|register|"
                   r"delete|remove|send|post|publish|submit|confirm|transfer|donate|book|reserve|unsubscribe|"
                   r"acheter|payer|commander|supprimer|envoyer|publier|s'inscrire|comprar|pagar|enviar|eliminar)\b",
                   re.I)

# The page as text: headings, paragraphs and list items on their own lines, every interactive element
# numbered in place. The numbers are stored on the elements (data-hl-ref) so a later click finds them.
SNAPSHOT_JS = r"""(rich) => {
  const MAX = 400; let n = 0; const lines = []; let line = '';
  document.querySelectorAll('[data-hl-ref]').forEach(e => e.removeAttribute('data-hl-ref'));
  const flush = () => { const t = line.replace(/\s+/g, ' ').trim(); if (t) lines.push(t); line = ''; };
  const hidden = (el) => {
    if (el.hidden || el.getAttribute('aria-hidden') === 'true') return true;
    if (el.tagName === 'INPUT' && el.type === 'hidden') return true;
    try { const s = getComputedStyle(el); if (s.display === 'none' || s.visibility === 'hidden') return true; } catch (e) {}
    return false;
  };
  const label = (el) => {
    let t = el.getAttribute('aria-label') || '';
    if (!t && el.labels && el.labels.length) t = el.labels[0].innerText || el.labels[0].textContent || '';
    if (!t) t = el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('alt') || '';
    return t.replace(/\s+/g, ' ').trim();
  };
  const own = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const BLOCK = new Set(['P','DIV','SECTION','ARTICLE','HEADER','FOOTER','NAV','MAIN','ASIDE','UL','OL','LI','TR',
    'TABLE','FORM','FIELDSET','PRE','BLOCKQUOTE','H1','H2','H3','H4','H5','H6','BR','HR','DL','DT','DD','FIGURE']);
  const interactive = (el) => {
    const t = el.tagName;
    if (t === 'A' && el.getAttribute('href')) return true;
    if (['BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY'].includes(t)) return true;
    const r = el.getAttribute('role');
    if (r && ['button','link','checkbox','radio','tab','menuitem','option','switch','combobox','textbox'].includes(r)) return true;
    return el.hasAttribute('onclick') || el.getAttribute('contenteditable') === 'true';
  };
  const describe = (el, ref) => {
    const t = el.tagName; const typ = (el.getAttribute('type') || '').toLowerCase();
    const name = el.getAttribute('name') ? ` name=${el.getAttribute('name')}` : '';
    if (t === 'A') { const h = el.getAttribute('href') || ''; return `[${ref}] link "${(own(el) || label(el)).slice(0, 80)}" → ${h.slice(0, 120)}`; }
    if (t === 'SELECT') {
      const opts = Array.from(el.options || []).slice(0, 25).map(o => `${o.value}=${(o.text || '').trim().slice(0, 30)}${o.selected ? '*' : ''}`);
      return `[${ref}] select${name} "${label(el)}": ${opts.join(', ')}`;
    }
    if (t === 'TEXTAREA') return `[${ref}] textarea${name} "${label(el)}" value="${(el.value || '').slice(0, 60)}"`;
    if (t === 'INPUT') {
      if (['submit','button','reset','image'].includes(typ)) return `[${ref}] button "${el.value || label(el) || typ}"`;
      if (['checkbox','radio'].includes(typ)) return `[${ref}] ${typ}${name} "${label(el)}" ${el.checked ? '[x]' : '[ ]'}`;
      const v = typ === 'password' ? (el.value ? '•••' : '') : (el.value || '').slice(0, 60);
      return `[${ref}] input(${typ || 'text'})${name} "${label(el)}" value="${v}"`;
    }
    const role = el.getAttribute('role');
    return `[${ref}] ${t === 'BUTTON' ? 'button' : (role || t.toLowerCase())} "${(own(el) || label(el)).slice(0, 80)}"`;
  };
  const walk = (node) => {
    for (const c of node.childNodes) {
      if (c.nodeType === 3) { line += ' ' + c.textContent; continue; }
      if (c.nodeType !== 1) continue;
      const tag = c.tagName;
      if (['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','IFRAME'].includes(tag) || hidden(c)) continue;
      if (interactive(c) && n < MAX) {
        n++; c.setAttribute('data-hl-ref', String(n));
        line += ' ' + describe(c, n) + ' ';
        if (tag !== 'SELECT' && !['A','BUTTON'].includes(tag)) continue;
        continue;
      }
      const block = BLOCK.has(tag);
      if (block) flush();
      if (/^H[1-6]$/.test(tag)) line += '#'.repeat(+tag[1]) + ' ';
      if (tag === 'LI') line += '- ';
      walk(c);
      if (block) flush();
    }
  };
  if (document.body) walk(document.body);
  flush();
  const text = lines.filter((l, i) => l !== lines[i - 1]).join('\n');
  return {url: location.href, title: document.title || '', text: text, refs: n,
          scripts: document.scripts ? document.scripts.length : 0};
}"""

INFO_JS = r"""(ref) => {
  const el = document.querySelector(`[data-hl-ref="${ref}"]`);
  if (!el) return null;
  const typ = (el.getAttribute('type') || '').toLowerCase();
  const inForm = !!(el.form || el.closest('form'));
  const submits = (el.tagName === 'BUTTON' && inForm && (!typ || typ === 'submit')) ||
                  (el.tagName === 'INPUT' && ['submit','image'].includes(typ));
  return {tag: el.tagName.toLowerCase(), type: typ, inForm: inForm, submits: submits,
          text: ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').replace(/\s+/g, ' ').trim().slice(0, 80),
          href: el.getAttribute('href') || ''};
}"""


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def chromium_available() -> bool:
    """A Chromium-family browser on this computer, or one Playwright installed for itself."""
    from pathlib import Path
    from .vision import _browsers
    if _browsers():
        return True
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "", str(Path.home() / ".cache" / "ms-playwright"),
             str(Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"), str(Path.home() / "Library/Caches/ms-playwright")]
    return any(Path(r).is_dir() and any(Path(r).glob("chromium*")) for r in roots if r)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Session:
    """One page, driven from its own thread (Playwright's sync API is bound to the thread that started it)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.engine = ""
        self.lp_proc: Optional[subprocess.Popen] = None
        self.browser = self.context = self.page = None
        self.last = ""                       # the last snapshot text, for action="read"
        self.allowed = False                 # "allow for this session" on a risky action
        self.used = time.monotonic()
        self.closed = False

    # ── the browser thread ────────────────────────────────────────────
    def call(self, fn: Callable[[], Any], timeout: float = 90) -> Any:
        if self.closed:
            raise RuntimeError("the browser was closed")
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, name="hermes-browser", daemon=True)
            self.thread.start()
        box: dict = {}
        done = threading.Event()
        self.jobs.put((fn, box, done))
        if not done.wait(timeout):
            raise TimeoutError(f"the browser did not answer within {int(timeout)} s")
        self.used = time.monotonic()
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _loop(self) -> None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            self.p = p
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                fn, box, done = job
                try:
                    box["value"] = fn()
                except BaseException as exc:  # handed back to the caller
                    box["error"] = exc
                done.set()
            self._shutdown_engine()

    # ── engines ───────────────────────────────────────────────────────
    def start(self, engine: str) -> None:
        """Runs on the browser thread."""
        self._shutdown_engine()
        if engine == "lightpanda":
            from .web import lightpanda
            exe = lightpanda(self.cfg)
            if not exe:
                raise RuntimeError("Lightpanda is not installed")
            port = _free_port()
            from .. import oscompat
            self.lp_proc = subprocess.Popen([exe, "serve", "--host", "127.0.0.1", "--port", str(port)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            stdin=subprocess.DEVNULL, **oscompat.new_group_kwargs())
            deadline = time.monotonic() + 8
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                    break
                except OSError:
                    if time.monotonic() > deadline or self.lp_proc.poll() is not None:
                        raise RuntimeError("Lightpanda did not start")
                    time.sleep(0.1)
            self.browser = self.p.chromium.connect_over_cdp(f"ws://127.0.0.1:{port}")
            self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        else:
            from .vision import _browsers
            paths = [path for name, path in _browsers()]
            kw = {"headless": True, "args": ["--disable-gpu", "--no-first-run"]}
            last: Optional[Exception] = None
            for path in paths + [None]:              # None: a browser Playwright installed itself, if any
                try:
                    self.browser = self.p.chromium.launch(executable_path=path, **kw) if path else \
                        self.p.chromium.launch(**kw)
                    break
                except Exception as exc:
                    last = exc
            else:
                raise RuntimeError(f"no Chromium-family browser could be started ({short(str(last), 160)})")
            self.context = self.browser.new_context(accept_downloads=False, viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.page.set_default_timeout(15000)
        self.engine = engine

    def _shutdown_engine(self) -> None:
        for close in (lambda: self.browser and self.browser.close(),):
            try:
                close()
            except Exception:
                pass
        self.browser = self.context = self.page = None
        if self.lp_proc and self.lp_proc.poll() is None:
            from .. import oscompat
            oscompat.kill_tree(self.lp_proc.pid, grace=0.5)
        self.lp_proc = None
        self.engine = ""

    def close(self) -> None:
        if self.thread and not self.closed:
            self.jobs.put(None)
            self.thread.join(timeout=10)
        self.closed = True

    # ── page helpers (browser thread) ─────────────────────────────────
    def settle(self) -> None:
        for state, ms in (("load", 15000), ("networkidle", 2500)):
            try:
                self.page.wait_for_load_state(state, timeout=ms)
            except Exception:
                pass
        time.sleep(0.2)

    def snapshot(self) -> dict:
        snap = self.page.evaluate(SNAPSHOT_JS, self.engine == "chromium")
        self.last = snap.get("text") or ""
        return snap


_SESSIONS: dict[int, Session] = {}
_LOCK = threading.Lock()


def _session(ctx: ToolContext) -> Session:
    with _LOCK:
        s = ctx.state.get("browser")
        if s is None or s.closed or (time.monotonic() - s.used > 60 * float(
                ctx.cfg.get_path("web.browser_idle_minutes", IDLE_MINUTES) or IDLE_MINUTES) and s.thread):
            if s is not None:
                s.close()
            s = ctx.state["browser"] = Session(ctx.cfg)
            _SESSIONS[id(s)] = s
        return s


def close_for(ctx: ToolContext) -> None:
    s = ctx.state.pop("browser", None)
    if s is not None:
        s.close()
        _SESSIONS.pop(id(s), None)


@atexit.register
def _close_all() -> None:
    for s in list(_SESSIONS.values()):
        try:
            s.close()
        except Exception:
            pass


def _engine_order(cfg) -> list[str]:
    choice = str(cfg.get_path("web.browser", "auto") or "auto").lower()
    if choice == "chromium":
        return ["chromium"]
    if choice == "lightpanda":
        return ["lightpanda"]
    from .web import lightpanda
    return (["lightpanda"] if lightpanda(cfg) else []) + (["chromium"] if chromium_available() else [])


def _result(s: Session, snap: dict, offset: int = 0, note: str = "") -> str:
    text = snap.get("text") or s.last
    part = text[offset:offset + SNAPSHOT_CHARS]
    more = len(text) - (offset + len(part))
    if more > 0:
        part += f"\n… {more} more characters: browser(action='read', offset={offset + len(part)})"
    data: dict = {"url": snap.get("url"), "title": snap.get("title"), "engine": s.engine, "page": part or "(empty page)"}
    if note:
        data["note"] = note
    return ok(**data)


def _approve(ctx: ToolContext, s: Session, what: str, reason: str) -> bool:
    if s.allowed or ctx.cfg.get_path("agent.approvals", "smart") == "off":
        return True
    verdict = ctx.approve(what, reason)
    if verdict == "session":
        s.allowed = True
    return verdict in ("once", "session")


def browser(args: dict, ctx: ToolContext) -> str:
    action = str(args.get("action") or ("open" if args.get("url") else "")).strip().lower()
    s = _session(ctx)
    if action == "close":
        close_for(ctx)
        return ok(success=True, note="Browser closed.")
    if action == "open":
        url = str(args.get("url") or "").strip()
        if not url:
            return error("open needs a url.")
        if not re.match(r"^(https?|file)://", url):
            url = "http://" + url if url.startswith(("localhost", "127.")) else "https://" + url
        return _open(s, url, ctx)
    if not s.page and action != "open":
        return error("No page is open: start with browser(action='open', url=...).")
    if action == "read":
        return _result(s, {"text": s.last, **_where(s)}, int(args.get("offset") or 0))
    if action in ("click", "type", "select"):
        return _act(s, action, args, ctx)
    if action in ("back", "forward", "reload"):
        def go():
            {"back": s.page.go_back, "forward": s.page.go_forward, "reload": s.page.reload}[action](wait_until="load")
            s.settle()
            return s.snapshot()
        return _guarded(s, go)
    if action == "scroll":
        return _scroll(s, ctx, str(args.get("direction") or "down").lower() != "up")
    return error(f"Unknown action {action!r}: open, click, type, select, scroll, back, forward, reload, read, close.")


GROWTH_JS = "() => document.body ? document.body.getElementsByTagName('*').length : 0"


def _scroll(s: Session, ctx: ToolContext, down: bool) -> str:
    """The snapshot already holds the whole page, so scrolling is for pages that load more as you go:
    jump to the bottom and wait until new content arrives (3 s at most). Lightpanda lays nothing out,
    so such pages never grow there: they move to Chromium."""
    def scroll():
        before = s.page.evaluate(GROWTH_JS)
        s.page.evaluate("(d) => { const h = Math.max(document.body ? document.body.scrollHeight : 0,"
                        " document.documentElement.scrollHeight); window.scrollTo(0, d ? h : 0);"
                        " const last = d && document.body && document.body.lastElementChild;"
                        " if (last && last.scrollIntoView) last.scrollIntoView(false); }", down)
        grew = False
        if down:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                time.sleep(0.2)
                if s.page.evaluate(GROWTH_JS) > before:
                    grew = True
                    break
        s.settle()
        snap = s.snapshot()
        snap["grew"] = grew
        return snap
    try:
        snap = s.call(scroll)
    except Exception as exc:
        return error(f"The browser ({s.engine}) failed: {short(str(exc), 300)}")
    if not down:
        return _result(s, snap, note="Back at the top.")
    if snap["grew"]:
        return _result(s, snap, note="Scrolled to the bottom: more content loaded (it is in this snapshot).")
    if s.engine == "lightpanda" and "chromium" in _engine_order(ctx.cfg):
        url = snap.get("url") or ""
        opened = json.loads(_open(s, url, ctx, note="")) if url and _force_chromium(s, ctx) else {}
        if opened.get("engine") == "chromium":
            again = json.loads(_scroll(s, ctx, True))
            again["note"] = ("Lightpanda cannot load content that appears on scroll, so this page is now open in "
                             "Chromium. " + again.get("note", "")).strip()
            return json.dumps(again)
    return _result(s, snap, note="Scrolled to the bottom: nothing more loaded (the snapshot already holds the "
                                 "whole page).")


def _force_chromium(s: Session, ctx: ToolContext) -> bool:
    try:
        s.call(lambda: s.start("chromium"))
        return True
    except Exception:
        return False


def _where(s: Session) -> dict:
    try:
        return s.call(lambda: {"url": s.page.evaluate("() => location.href"), "title": s.page.title()}, 20)
    except Exception:
        return {"url": "", "title": ""}


def _guarded(s: Session, fn: Callable[[], dict], note: str = "") -> str:
    try:
        return _result(s, s.call(fn), note=note)
    except Exception as exc:
        return error(f"The browser ({s.engine}) failed: {short(str(exc), 300)}")


def _open(s: Session, url: str, ctx: ToolContext, note: str = "") -> str:
    tried = []
    order = _engine_order(ctx.cfg)
    if not order:
        return error("No browser to drive: install Lightpanda (pip install lightpanda in Hermes Lite's venv) or "
                     "Chromium.")
    if s.engine == "chromium" and "chromium" in order:   # once a site needed Chromium, stay on it
        order = ["chromium"]
    for engine in order:
        def go(engine=engine):
            if s.engine != engine or not s.page:
                s.start(engine)
            s.page.goto(url, wait_until="load", timeout=30000)
            s.settle()
            snap = s.snapshot()
            if engine == "lightpanda" and len(snap.get("text") or "") < 40 and snap.get("scripts", 0) > 0:
                raise RuntimeError("the page came out empty (it may need a full browser)")
            return snap
        try:
            snap = s.call(go)
            if tried:
                note = (note + " " if note else "") + f"Lightpanda could not handle this page ({tried[-1]}); opened it in Chromium."
            return _result(s, snap, note=note)
        except Exception as exc:
            tried.append(short(str(exc), 200))
    return error(f"Could not open {url}: " + " | ".join(tried))


def _act(s: Session, action: str, args: dict, ctx: ToolContext) -> str:
    try:
        ref = int(str(args.get("ref") or "").strip().lstrip("[").rstrip("]"))
    except ValueError:
        return error(f"{action} needs ref: the number shown in the snapshot, e.g. ref=12 for [12].")
    try:
        info = s.call(lambda: s.page.evaluate(INFO_JS, ref), 20)
    except Exception as exc:
        return error(f"The browser failed: {short(str(exc), 300)}")
    if not info:
        return error(f"There is no [{ref}] on this page any more (it changed): read the latest snapshot first.")
    where = _where(s).get("url", "")
    target = f"[{ref}] {info['tag']} \"{info['text']}\""
    if action == "type":
        if info["type"] == "password":
            return error("Passwords are never typed by Hermes Lite. This throwaway browser has no logins: pages behind "
                         "a sign-in are out of reach — tell the user, or find the information elsewhere.")
        text = str(args.get("text") if args.get("text") is not None else "")
        submit = bool(args.get("submit"))
        if submit and info["inForm"] and not _approve(ctx, s, f"browser: type into {target} and submit the form on {where}",
                                                      "submits a form on a web page"):
            return error("The user did not allow submitting this form.")

        def typing():
            loc = s.page.locator(f'[data-hl-ref="{ref}"]')
            try:
                loc.fill(text, timeout=5000)
            except Exception:
                s.page.evaluate("([r, v]) => { const e = document.querySelector(`[data-hl-ref=\"${r}\"]`); e.value = v;"
                                " e.dispatchEvent(new Event('input', {bubbles: true}));"
                                " e.dispatchEvent(new Event('change', {bubbles: true})); }", [ref, text])
            if submit:
                try:
                    loc.press("Enter", timeout=5000)
                except Exception:
                    s.page.evaluate("(r) => { const e = document.querySelector(`[data-hl-ref=\"${r}\"]`);"
                                    " const f = e.form || e.closest('form'); if (f) f.requestSubmit ? f.requestSubmit() : f.submit(); }", ref)
                time.sleep(0.3)
            s.settle()
            return s.snapshot()
        return _guarded_action(s, typing, ctx, f"Typed into [{ref}]" + (" and submitted." if submit else "."))
    if action == "select":
        value = str(args.get("value") or args.get("text") or "")

        def choose():
            loc = s.page.locator(f'[data-hl-ref="{ref}"]')
            # its value, its exact text, else the first option whose text contains it (any case)
            found = s.page.evaluate("([r, v]) => { const e = document.querySelector(`[data-hl-ref=\"${r}\"]`);"
                                    " const o = Array.from(e.options || []); const l = v.toLowerCase();"
                                    " const hit = o.find(x => x.value === v) || o.find(x => x.text.trim() === v) ||"
                                    " o.find(x => x.text.toLowerCase().includes(l)); return hit ? hit.value : null; }",
                                    [ref, value])
            if found is None:
                raise LookupError(f"[{ref}] has no option {value!r}")
            try:
                loc.select_option(found, timeout=5000)
            except Exception:
                s.page.evaluate("([r, v]) => { const e = document.querySelector(`[data-hl-ref=\"${r}\"]`); e.value = v;"
                                " e.dispatchEvent(new Event('change', {bubbles: true})); }", [ref, found])
            s.settle()
            return s.snapshot()
        return _guarded_action(s, choose, ctx, f"Chose {value!r} in [{ref}].")
    # click
    risky = info["submits"] or bool(RISKY.search(info["text"]))
    if risky and not _approve(ctx, s, f"browser: click {target} on {where}",
                              "submits a form" if info["submits"] else "may buy, send, delete or sign up"):
        return error("The user did not allow this click.")

    def click():
        before = s.page.evaluate("() => location.href")
        loc = s.page.locator(f'[data-hl-ref="{ref}"]')
        try:
            loc.click(timeout=5000)
        except Exception:
            s.page.evaluate("(r) => document.querySelector(`[data-hl-ref=\"${r}\"]`).click()", ref)
        time.sleep(0.3)
        s.settle()
        snap = s.snapshot()
        snap["moved"] = snap.get("url") != before
        return snap
    return _guarded_action(s, click, ctx, f"Clicked [{ref}].")


def _guarded_action(s: Session, fn: Callable[[], dict], ctx: ToolContext, done: str) -> str:
    try:
        snap = s.call(fn)
        return _result(s, snap, note=done + (" A new page loaded." if snap.get("moved") else ""))
    except LookupError as exc:
        return error(str(exc))
    except Exception as exc:
        if s.engine == "lightpanda" and "chromium" in _engine_order(ctx.cfg):
            url = _where(s).get("url") or ""
            if url:   # Lightpanda could not do it: same page in Chromium, and the model tries again there
                res = _open(s, url, ctx, note=f"Lightpanda could not do that ({short(str(exc), 160)}): this page is "
                                              "now open in Chromium. The numbers changed: read this snapshot and try again.")
                return res
        return error(f"The browser ({s.engine}) failed: {short(str(exc), 300)}")


if available():
    register(Tool(
        name="browser", emoji="🧭",
        schema={"name": "browser", "description": (
            "A web page that stays open between calls, to interact with sites: click links and buttons, type "
            "into search boxes and forms, choose options, scroll, go back. Each result is the page as text with "
            "every link, button and field numbered like [12]; act on those numbers. To just read pages and "
            "follow links, web_extract is cheaper; use browser when you must click, type or keep a page's "
            "state. A throwaway browser: no logins, never passwords. Submitting forms or clicking buy/send/"
            "delete may ask the user first."),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["open", "click", "type", "select", "scroll", "back", "forward",
                                                      "reload", "read", "close"]},
                "url": {"type": "string", "description": "open: the address."},
                "ref": {"type": "integer", "description": "click/type/select: the element's number from the snapshot."},
                "text": {"type": "string", "description": "type: what to type (replaces the field's content)."},
                "submit": {"type": "boolean", "description": "type: press Enter afterwards (submits a search or form)."},
                "value": {"type": "string", "description": "select: the option's value or its visible text."},
                "direction": {"type": "string", "enum": ["down", "up"], "description": "scroll: down = to the "
                              "bottom, for pages that load more as you scroll (the snapshot already holds the rest)."},
                "offset": {"type": "integer", "description": "read: continue the page text from this character."},
            }, "required": ["action"]}},
        handler=browser,
        label=lambda a: short(f"{a.get('action', '')} {a.get('url') or (('[' + str(a.get('ref')) + ']') if a.get('ref') is not None else '')} "
                              f"{a.get('text') or a.get('value') or ''}".strip(), 80),
    ))
