"""browser tool, for real: Lightpanda first, Chromium as the fallback, against a small local site."""
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from hermes_lite.config import load_config
from hermes_lite.tools import browser as br
from hermes_lite.tools.base import ToolContext
from hermes_lite.tools.vision import _browsers
from hermes_lite.tools.web import lightpanda

SITE = Path(__file__).parent / "browser_site"
pytestmark = pytest.mark.skipif(not br.available(), reason="playwright is not installed")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SITE)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _ctx(tmp_path, engine, approvals="smart", answer="once"):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg["web"]["browser"] = engine
    cfg["agent"]["approvals"] = approvals
    asked = []

    def approve(what, reason):
        asked.append((what, reason))
        return answer
    ctx = ToolContext(cfg=cfg, home=tmp_path, cwd=str(tmp_path), session_id="s", approve=approve)
    return ctx, asked


def call(ctx, **args):
    return json.loads(br.browser(args, ctx))


def ref_of(page: str, needle: str) -> int:
    line = next(l for l in page.splitlines() if needle in l)
    i = line.index(needle)
    start = line.rfind("[", 0, i)
    return int(line[start + 1:line.index("]", start)])


ENGINES = [pytest.param("lightpanda", marks=pytest.mark.skipif(not lightpanda(load_config("/nonexistent.yaml")),
                                                               reason="no Lightpanda")),
           pytest.param("chromium", marks=pytest.mark.skipif(not br.chromium_available(), reason="no Chromium"))]


@pytest.mark.parametrize("engine", ENGINES)
def test_browse_click_type_back(tmp_path, site, engine):
    ctx, asked = _ctx(tmp_path, engine)
    try:
        r = call(ctx, action="open", url=site + "/index.html")
        assert r["engine"] == engine and r["title"] == "Shop home"
        assert "Rendered by JavaScript" in r["page"] and "# Welcome" in r["page"]
        r = call(ctx, action="click", ref=ref_of(r["page"], 'button "Load more"'))
        assert "Loaded by JS click" in r["page"] and not asked          # an ordinary button: no question
        r = call(ctx, action="click", ref=ref_of(r["page"], 'link "Search"'))
        assert r["title"] == "Search" and r["url"].endswith("/search.html") and "A new page loaded" in r["note"]
        # typing never asks; submitting a form does
        r = call(ctx, action="type", ref=ref_of(r["page"], 'name=q'), text="hello world", submit=True)
        assert "You searched: hello world" in r["page"] and len(asked) == 1 and "submit" in asked[0][1]
        r = call(ctx, action="back")
        assert r["url"].endswith("/search.html")
        r = call(ctx, action="select", ref=ref_of(r["page"], "select name=lang"), value="Français")
        assert "fr=Français*" in r["page"]
        err = call(ctx, action="type", ref=ref_of(r["page"], "input(password)"), text="hunter2")["error"]
        assert "never typed" in err
        r2 = call(ctx, action="click", ref=ref_of(r["page"], 'button "Buy now"'))
        assert len(asked) == 2 and "buy" in asked[1][1] and "page" in r2
        assert "no [999]" in call(ctx, action="click", ref=999)["error"]
    finally:
        br.close_for(ctx)


@pytest.mark.parametrize("engine", ENGINES)
def test_js_app_keeps_state_between_calls(tmp_path, site, engine):
    ctx, _ = _ctx(tmp_path, engine)
    try:
        r = call(ctx, action="open", url=site + "/spa.html")
        r = call(ctx, action="type", ref=ref_of(r["page"], '"New item"'), text="milk")
        r = call(ctx, action="click", ref=ref_of(r["page"], 'button "Add"'))
        r = call(ctx, action="type", ref=ref_of(r["page"], '"New item"'), text="eggs")
        r = call(ctx, action="click", ref=ref_of(r["page"], 'button "Add"'))
        assert "- milk" in r["page"] and "- eggs" in r["page"]
    finally:
        br.close_for(ctx)


def test_denied_and_session_approvals(tmp_path, site):
    ctx, asked = _ctx(tmp_path, "auto", answer="deny")
    try:
        r = call(ctx, action="open", url=site + "/search.html")
        assert "did not allow" in call(ctx, action="click", ref=ref_of(r["page"], 'button "Buy now"'))["error"]
        ctx.approve = lambda what, reason: asked.append(what) or "session"
        call(ctx, action="click", ref=ref_of(r["page"], 'button "Buy now"'))
        call(ctx, action="click", ref=ref_of(call(ctx, action="read")["page"], 'button "Buy now"'))
        assert len(asked) == 2                                 # "for this session": asked once more, then not
    finally:
        br.close_for(ctx)
    off, asked = _ctx(tmp_path, "auto", approvals="off")
    try:
        r = call(off, action="open", url=site + "/search.html")
        call(off, action="click", ref=ref_of(r["page"], 'button "Buy now"'))
        assert not asked
    finally:
        br.close_for(off)


def test_long_pages_and_errors(tmp_path, site, monkeypatch):
    ctx, _ = _ctx(tmp_path, "auto")
    monkeypatch.setattr(br, "SNAPSHOT_CHARS", 40)
    try:
        assert "No page is open" in call(ctx, action="click", ref=1)["error"]
        r = call(ctx, action="open", url=site + "/index.html")
        assert "more characters: browser(action='read', offset=40)" in r["page"]
        r2 = call(ctx, action="read", offset=40)
        assert r2["page"] and not r2["page"].startswith(r["page"][:40])
        assert "Could not open" in call(ctx, action="open", url="http://127.0.0.1:9/nothing")["error"]
        assert call(ctx, action="close")["success"]
    finally:
        br.close_for(ctx)


@pytest.mark.skipif(not br.chromium_available(), reason="no Chromium")
def test_falls_back_to_chromium_when_lightpanda_fails(tmp_path, site, monkeypatch):
    ctx, _ = _ctx(tmp_path, "auto")
    real_start = br.Session.start

    def broken_lightpanda(self, engine):
        if engine == "lightpanda":
            raise RuntimeError("simulated Lightpanda failure")
        return real_start(self, engine)
    monkeypatch.setattr(br.Session, "start", broken_lightpanda)
    monkeypatch.setattr(br, "_engine_order", lambda cfg: ["lightpanda", "chromium"])
    try:
        r = call(ctx, action="open", url=site + "/docs.html")
        assert r["engine"] == "chromium" and "simulated Lightpanda failure" in r["note"]
        assert "Documentation" in r["page"]
    finally:
        br.close_for(ctx)


@pytest.mark.skipif(not br.chromium_available(), reason="no Chromium")
def test_scroll_loads_more_and_lightpanda_hands_over(tmp_path, site):
    ctx, _ = _ctx(tmp_path, "auto")
    try:
        r = call(ctx, action="open", url=site + "/feed.html")
        seen = [r["page"].count("Post ")]
        notes = []
        for _ in range(3):
            r = call(ctx, action="scroll", direction="down")
            seen.append(r["page"].count("Post "))
            notes.append(r["note"])
        assert seen[-1] == 60 and r["engine"] == "chromium"           # the whole feed, loaded by scrolling
        if seen[0] == 40:                                              # Lightpanda first: it handed the page over
            assert "now open in Chromium" in notes[0]
        assert "nothing more loaded" in notes[-1]
        assert "Back at the top" in call(ctx, action="scroll", direction="up")["note"]
    finally:
        br.close_for(ctx)
