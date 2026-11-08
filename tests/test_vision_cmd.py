"""/vision ram|vram|off edits config.yaml (one line each) and the live config."""
import asyncio
from types import SimpleNamespace

from hermes_lite.config import load_config
from hermes_lite.server import build_command, vision_device
from hermes_lite.tui.app import HermesApp


def _fake(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    (tmp_path / "mmproj-F16.gguf").write_bytes(b"x")
    f = tmp_path / "config.yaml"
    f.write_text(f"server:\n  model_path: {model}\n")
    lines = []
    app = SimpleNamespace(cfg=load_config(str(f)), start_server=False, lines=lines,
                          _line=lambda text, kind="": lines.append((kind, text)),
                          agent=SimpleNamespace(llm=SimpleNamespace(vision=None), busy=False))
    return app, f


def test_vision_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    app, f = _fake(tmp_path)
    run = lambda arg: asyncio.run(HermesApp._vision(app, arg))
    run("")
    assert "off (server.mmproj: none)" in app.lines[-1][1]
    run("ram")
    text = f.read_text()
    assert "mmproj_device: ram" in text and "mmproj: auto" in text
    assert vision_device(app.cfg) == "ram" and vision_device(load_config(str(f))) == "ram"
    assert "--no-mmproj-offload" in build_command(app.cfg)
    run("vram")
    assert "mmproj_device: vram" in f.read_text() and "--no-mmproj-offload" not in build_command(app.cfg)
    run("off")
    assert "mmproj: none" in f.read_text() and "--mmproj" not in build_command(app.cfg)
    assert "model_path" in f.read_text()                 # the rest of the file is kept
