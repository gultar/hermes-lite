"""A restart (/server restart, /vision ...) must bring the server back. The previous launch script
finishes after its llama-server exits; it used to delete the new server's pid file and report it
dead, so the restart stopped the server instead."""
import asyncio
import os
import subprocess
import sys

import pytest

from hermes_lite import server
from hermes_lite.config import load_config
from hermes_lite.server import LlamaServer

FAKE = r'''#!/usr/bin/env python3
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
a = sys.argv
if "--help" in a:
    print("-ngl --n-cpu-moe -c -ub"); sys.exit(0)
print("llama_server: model loaded", flush=True)
class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_GET(self):
        b = json.dumps({"status": "ok"}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer(("127.0.0.1", int(a[a.index("--port") + 1])), H).serve_forever()
'''
# a slow `tee`: the old launch script finishes a second after its server is gone (a busy, swapping machine)
SLOW_TEE = "#!/usr/bin/env bash\n/usr/bin/tee \"$@\"\nsleep 1\n"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses the Linux launch script")
def test_restart_brings_the_server_back(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("llama-server", FAKE), ("tee", SLOW_TEE)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    (tmp_path / "model.gguf").write_bytes(b"x")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    server._supports.cache_clear()
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg["server"].update(binary="llama-server", model_path=str(tmp_path / "model.gguf"), port=18932,
                         terminal="none", stop_others=[], ctx_size=40000)
    srv = LlamaServer(cfg)

    async def go():
        assert await srv.ensure() is True
        first = srv.pid()
        await srv.stop_any()                      # what /vision and /server restart do
        assert await srv.ensure() is True
        await asyncio.sleep(1.5)                  # the old script has finished by now
        return first, srv.pid(), await srv.health()
    try:
        first, second, health = asyncio.run(go())
        assert second and second != first and health == "ready"
        assert not srv.exitfile.exists()
    finally:
        subprocess.run(["pkill", "-f", str(bindir / "llama-server")])
        server._supports.cache_clear()
