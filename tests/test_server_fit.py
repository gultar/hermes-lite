"""When llama-server can't get the GPU memory, Hermes Lite lowers the context 1k at a time and
never touches the micro-batch — also when llama.cpp dies without logging why."""
import asyncio
import os
import subprocess
import sys

import pytest

from hermes_lite import server
from hermes_lite.config import load_config
from hermes_lite.server import LlamaServer, build_command, ladder_for

# Dies like llama.cpp after a failed GPU allocation: abort, its error line never flushed.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
a = sys.argv
if "--help" in a:
    print("-ngl --n-cpu-moe -c -ub --fit [on|off]"); sys.exit(0)
ctx = int(a[a.index("-c") + 1]); ub = int(a[a.index("-ub") + 1])
print("0.00.154.373 I srv  llama_server: initializing ...", flush=True)
print(f"0.00.156.976 I srv    load_model: loading model '{a[a.index('-m') + 1]}'", flush=True)
assert ub == 1024, "the micro-batch must never change"
if ctx > 63000:
    os.abort()
port = int(a[a.index("--port") + 1])
print("0.09.862.197 I srv  llama_server: model loaded", flush=True)
class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_GET(self):
        b = json.dumps({"status": "ok"}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _cfg(tmp_path, **server_settings):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg["server"].update(**server_settings)
    return cfg


def test_ladder_keeps_the_micro_batch_and_steps_the_context_by_1k(tmp_path):
    cfg = _cfg(tmp_path, ctx_size=66000, extra_args=["-ub", "1024"], n_cpu_moe=99)
    rungs = ladder_for(cfg)
    assert rungs[0]["label"] == "66,000-token context, micro-batch 1024 (your settings)"
    assert [r["ctx"] for r in rungs[1:4]] == [65536, 64512, 63488]
    for i in range(len(rungs)):
        cmd = build_command(cfg, i)
        assert cmd[cmd.index("-ub") + 1] == "1024"
    assert min(r["ctx"] for r in rungs) >= 32768


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses the Linux launch script")
def test_silent_death_steps_down_to_what_fits(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "llama-server").write_text(FAKE)
    (bindir / "llama-server").chmod(0o755)
    (tmp_path / "model.gguf").write_bytes(b"x")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    server._supports.cache_clear()  # earlier tests looked up another "llama-server"
    cfg = _cfg(tmp_path, binary="llama-server", model_path=str(tmp_path / "model.gguf"), port=18931, terminal="none",
               ctx_size=66000, extra_args=["-ub", "1024"], stop_others=[], n_cpu_moe=99)
    srv = LlamaServer(cfg)
    try:
        assert asyncio.run(srv.ensure()) is True
        assert srv.rung_label().startswith("62,464-token context, micro-batch 1024")   # the first one under 63000
        cmd = build_command(cfg, srv.rung)
        assert cmd[cmd.index("--fit") + 1] == "off" and cmd[cmd.index("-ub") + 1] == "1024"
    finally:
        subprocess.run(["pkill", "-f", str(bindir / "llama-server")])
        server._supports.cache_clear()
