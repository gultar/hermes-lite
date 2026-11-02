"""sudo password bridge: sudo -A runs a tiny helper, the helper asks Hermes Lite over a private
Unix socket, Hermes Lite shows a masked password box, and the answer goes straight back to sudo.

The password never enters the conversation, the tool output, the logs or any file. It exists in
memory only while sudo reads it, unless the user ticks "remember for this session", in which case
it is kept in this process's memory until Hermes Lite quits.
"""

from __future__ import annotations

import contextvars
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path

HELPER = """#!{python}
import socket, sys
prompt = " ".join(sys.argv[1:]) or "Password:"
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect({sock!r})
    s.sendall(prompt.replace("\\n", " ").encode() + b"\\n")
    data = b""
    while not data.endswith(b"\\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
except OSError:
    sys.exit(1)
if not data.startswith(b"OK "):
    sys.exit(1)
sys.stdout.write(data[3:-1].decode())
sys.stdout.write("\\n")
"""


class _Bridge:
    def __init__(self, ctx):
        self.ctx = ctx
        self.dir = ctx.home / "scratch" / ctx.session_id / ".askpass"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.sock = self.dir / "s"
        self.helper = self.dir / "askpass"
        self.remembered: str | None = None
        self.lock = threading.Lock()
        # Created from a tool thread, which carries the UI's context (Textual's active app). The
        # socket thread does not, so prompts are raised inside a copy of this context.
        self.context = contextvars.copy_context()
        try:
            self.sock.unlink()
        except FileNotFoundError:
            pass
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                prompt = self.rfile.readline().decode(errors="replace").strip() or "Password:"
                with bridge.lock:  # one password box at a time
                    if bridge.remembered is not None:
                        answer = bridge.remembered
                    else:
                        answer, remember = bridge.context.copy().run(
                            bridge.ctx.ask_secret, prompt, bridge.ctx.state.get("current_command", ""))
                        if answer is not None and remember:
                            bridge.remembered = answer
                if answer is None:
                    self.wfile.write(b"NO\n")
                else:
                    self.wfile.write(b"OK " + answer.encode() + b"\n")

        self.server = socketserver.ThreadingUnixStreamServer(str(self.sock), Handler)
        os.chmod(self.sock, 0o600)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="askpass").start()
        self.helper.write_text(HELPER.format(python=sys.executable, sock=str(self.sock)), encoding="utf-8")
        os.chmod(self.helper, 0o700)

    def close(self):
        self.remembered = None
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        for p in (self.sock, self.helper):
            try:
                p.unlink()
            except OSError:
                pass


def helper_path(ctx) -> str | None:
    """Start the bridge once per session; None when the UI cannot ask for a password."""
    from .. import oscompat
    if ctx.ask_secret is None or oscompat.IS_WINDOWS:  # no sudo -A / askpass on Windows
        return None
    bridge = ctx.state.get("askpass")
    if bridge is None:
        try:
            bridge = _Bridge(ctx)
        except OSError:
            return None
        ctx.state["askpass"] = bridge
    return str(bridge.helper)


def forget(ctx) -> None:
    bridge = ctx.state.get("askpass")
    if bridge is not None:
        bridge.remembered = None


def close(ctx) -> None:
    bridge = ctx.state.pop("askpass", None)
    if bridge is not None:
        bridge.close()
