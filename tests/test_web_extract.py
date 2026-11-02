"""web_extract: a bigger budget, shared between several URLs, and a page's beginning plus where to continue."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermes_lite.config import load_config
from hermes_lite.tools import REGISTRY
from hermes_lite.tools.base import ToolContext

PARAS = [f"Paragraph {i}: " + "words about the music player API " * 12 for i in range(400)]
HTML = "<html><head><title>Docs</title></head><body><main>" + "".join(f"<p>{p}</p>" for p in PARAS) + "</main></body></html>"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path))
    cfg = load_config(str(tmp_path / "none.yaml"))
    return ToolContext(cfg=cfg, home=tmp_path, cwd=str(tmp_path), session_id="s1")


def test_one_page_gets_the_full_budget_and_a_place_to_continue(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/docs"
    ctx = _ctx(tmp_path, monkeypatch)
    page = json.loads(REGISTRY["web_extract"].handler({"urls": [url]}, ctx))["results"][0]
    assert 32000 < page["shown_chars"] <= 40000 and page["content"].startswith("Paragraph 0")
    full = open(page["full_text_file"]).read().splitlines()
    offset = int(page["continue"].split("offset=")[1].split()[0])
    last_shown = page["content"].rstrip().splitlines()[-1]
    nxt = next(l for l in full[offset - 1:] if l.strip())
    assert last_shown in full[:offset - 1] and nxt not in page["content"]       # continues right after
    several = json.loads(REGISTRY["web_extract"].handler({"urls": [url] * 3}, ctx))["results"]
    assert all(len(p["content"]) <= 20000 for p in several)                     # 60000 shared by 3
    srv.shutdown()


TUT = {
    "/": "<html><head><title>OS in Rust</title></head><body><nav><a href='/one/'>Chapter 1</a> <a href='/two/'>Chapter 2</a></nav>"
         "<main><h1>OS in Rust</h1></main></body></html>",
    "/one/": "<html><head><title>One</title><link rel='next' href='/two/'></head><body><nav><a href='/one/'>Chapter 1</a>"
             "<a href='/two/'>Chapter 2</a></nav><article><h1>One</h1><pre><code>cargo new blog_os</code></pre></article>"
             "<footer><a href='/'>← Previous</a></footer></body></html>",
}


class T(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = TUT.get(self.path, "<html><body>nothing</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_contents_next_and_navigation(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), T)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    ctx = _ctx(tmp_path, monkeypatch)
    toc = json.loads(REGISTRY["web_extract"].handler({"urls": [base + "/"], "links_only": True}, ctx))["results"][0]
    assert toc["links"] == [f"Chapter 1 — {base}/one/", f"Chapter 2 — {base}/two/"] and "content" not in toc
    one = json.loads(REGISTRY["web_extract"].handler({"urls": [base + "/one/"], "render": "never"}, ctx))["results"][0]
    assert one["next"] == base + "/two/" and one["previous"] == base + "/"
    assert "cargo new blog_os" in one["content"] and "Chapter 2" not in one["content"]   # menus stay out of the text
    assert f"Chapter 2 — {base}/two/" in one["navigation"]
    srv.shutdown()


JS = ("<html><head><title>JS docs</title><script>1</script><script>2</script><script>3</script></head><body>"
      "<nav><a href='/next/'>Next →</a></nav><div id=app></div><script>document.getElementById('app').innerHTML="
      "'<h1>Rendered by JavaScript</h1><p>'+'The kernel boots from the bootloader crate. '.repeat(20)+'</p>'</script></body></html>")


class J(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = JS.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_javascript_pages_run_in_lightpanda(tmp_path, monkeypatch):
    import os
    import pytest
    lp = os.environ.get("HL_TEST_LIGHTPANDA", "/tmp/lpvenv/bin/lightpanda")
    if not os.path.isfile(lp):
        pytest.skip("no lightpanda binary")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), J)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ctx = _ctx(tmp_path, monkeypatch)
    ctx.cfg["web"]["lightpanda"] = lp
    page = json.loads(REGISTRY["web_extract"].handler({"urls": [f"http://127.0.0.1:{srv.server_port}/"]}, ctx))["results"][0]
    assert page["rendered"].endswith("(Lightpanda)") and "Rendered by JavaScript" in page["content"]
    assert page["next"].endswith("/next/")                 # our own converter: links and next/previous still work
    srv.shutdown()


def test_urls_written_as_objects_and_odd_arguments(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), T)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    ctx = _ctx(tmp_path, monkeypatch)
    tool = REGISTRY["web_extract"]
    args = {"urls": [{"url": base + "/", "links_only": True}]}            # how the model wrote it
    assert tool.label(args) == base + "/"
    toc = json.loads(tool.handler(args, ctx))["results"][0]
    assert toc["links"][0].startswith("Chapter 1") and "content" not in toc
    assert json.loads(tool.handler({"urls": {"url": base + "/one/"}}, ctx))["results"][0]["title"] == "One"
    assert "give 'urls'" in json.loads(tool.handler({"urls": [{"nope": 1}]}, ctx))["error"]
    srv.shutdown()


def test_a_bug_in_a_tool_costs_one_turn_not_the_app(tmp_path, monkeypatch):
    import asyncio
    from hermes_lite.agent import Agent
    from hermes_lite.llm import ToolCall, Turn
    from hermes_lite.tools.base import Tool

    events = []

    class UI:
        def event(self, kind, **data):
            events.append((kind, data))

    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path))
    a = Agent(load_config(str(tmp_path / "none.yaml")), UI(), cwd=str(tmp_path))
    boom = REGISTRY["web_extract"]
    monkeypatch.setitem(REGISTRY, "web_extract", Tool(name="web_extract", schema=boom.schema, handler=boom.handler,
                                                     label=lambda a: 1 / 0))   # a label that crashes
    calls = iter([Turn(tool_calls=[ToolCall(id="c1", name="web_extract", arguments='{"urls": ["x"]}',
                                            parsed={"urls": ["x"]})]), Turn(content="done")])

    async def fake(messages, tools, **kw):
        return next(calls)
    a.llm.stream = fake
    asyncio.run(a.run("go"))
    assert ("final", {"text": "done"}) in events or any(k == "error" for k, _ in events)
    assert not a.busy
