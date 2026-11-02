"""Big GitHub files work like big local files: outline + first lines, then any range, from a session copy."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermes_lite.config import load_config
from hermes_lite.tools import REGISTRY, research
from hermes_lite.tools.base import ToolContext

BIG = "".join(f"class Thing{i}:\n    def method_{i}(self, x: int) -> int:\n        return x + {i}\n\n" for i in range(150))
SMALL = "print('hello')\n"
HITS = []


class Raw(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
        body = (BIG if self.path.endswith("big.py") else SMALL).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_big_file_outline_then_ranges_from_the_session_copy(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Raw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(research, "RAW_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path))
    ctx = ToolContext(cfg=load_config(str(tmp_path / "none.yaml")), home=tmp_path, cwd=str(tmp_path), session_id="s1")
    gh = lambda **a: json.loads(REGISTRY["github"].handler({"action": "file", "repo": "phil-opp/blog_os", **a}, ctx))

    small = gh(path="src/small.py")
    assert small["content"] == SMALL and "outline" not in small               # under the limit: as before

    first = gh(path="src/big.py")
    assert "class Thing0" in first["outline"] and "L5-7" in first["outline"]        # the file's map, with line ranges
    assert first["content"].startswith("1|class Thing0:") and "601|" not in first["content"]
    assert "has 601 lines" in first["note"] and first["cached_copy"].endswith("big.py")
    downloads = len(HITS)

    part = gh(path="src/big.py", offset=597, limit=4)
    assert part["content"].splitlines()[0] == "597|class Thing149:" and len(HITS) == downloads   # no new download
    local = json.loads(REGISTRY["read_file"].handler({"path": first["cached_copy"], "offset": 5, "limit": 2}, ctx))
    assert local["content"].startswith("5|class Thing1:")
    srv.shutdown()
