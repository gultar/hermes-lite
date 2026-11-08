"""screenshot(target=window): works from what the desktop offers, not from its name. Stand-in grim,
lswt, swaymsg, hyprctl and xdotool record what they were asked."""
import json
import os
import sys

import pytest
from PIL import Image

from hermes_lite.config import load_config
from hermes_lite.tools import vision
from hermes_lite.tools.base import ToolContext

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux desktops")

EXT = {"version": 2, "toplevels": [
    {"title": "fish ~/proj", "app-id": "kitty", "identifier": "a1"},
    {"title": "Mozilla Firefox", "app-id": "firefox", "identifier": "b2"}]}
WLR = {"version": 2, "toplevels": [
    {"title": "fish ~/proj", "app-id": "kitty", "activated": False},
    {"title": "Mozilla Firefox", "app-id": "firefox", "activated": True}]}


def _tool(bindir, name, body):
    (bindir / name).write_text("#!/usr/bin/env bash\n" + body)
    (bindir / name).chmod(0o755)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    png = tmp_path / "shot.png"
    Image.new("RGB", (40, 30), "white").save(png)
    log = tmp_path / "calls.log"
    for k in ("WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    cfg = load_config(str(tmp_path / "none.yaml"))
    ctx = ToolContext(cfg=cfg, home=tmp_path / "home", cwd=str(tmp_path), session_id="s")

    def grim(toplevel=True):
        _tool(bindir, "grim", f'''if [ "$1" = -h ]; then echo "Usage: grim [-o output] [-g geometry] {'[-T identifier]' if toplevel else ''}"; exit 0; fi
echo "grim $*" >> {log}; for a; do last=$a; done; cp {png} "$last"''')

    def shot(target):
        res = json.loads(vision.screenshot({"target": target}, ctx))
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return res, calls
    return {"bin": bindir, "tool": lambda n, b: _tool(bindir, n, b), "grim": grim, "shot": shot,
            "env": monkeypatch, "ctx": ctx, "log": log}


def test_mangowm_style_compositor_with_lswt(desk):
    desk["env"].setenv("WAYLAND_DISPLAY", "wayland-1")
    desk["grim"]()
    desk["tool"]("lswt", f'''case "$*" in
  *wlr-foreign*) echo '{json.dumps(WLR)}' ;;
  *) echo '{json.dumps(EXT)}' ;;
esac''')
    res, calls = desk["shot"]("window")                 # the focused one, known from the wlr protocol
    assert res["success"] and calls[-1].startswith("grim -T b2 ")
    res, calls = desk["shot"]("kitty")
    assert calls[-1].startswith("grim -T a1 ") and "fish" in res["shows"]
    res, _ = desk["shot"]("blender")
    assert "No window matches" in res["error"] and "firefox" in res["error"]
    assert vision.window_capture_available()
    from hermes_lite.tools import REGISTRY
    assert "capture one window by name" in REGISTRY["screenshot"].openai_schema()["function"]["description"]


def test_no_window_support_falls_back_to_the_screen(desk):
    desk["env"].setenv("WAYLAND_DISPLAY", "wayland-1")
    desk["grim"](toplevel=False)                        # grim older than 1.5, and no lswt
    res, calls = desk["shot"]("window")
    assert res["success"] and res["shows"] == "the screen" and calls == [f"grim {calls[0].split()[-1]}"]
    assert "could not be captured here" in res["note"] and "lswt" in res["note"]
    assert not vision.window_capture_available()
    from hermes_lite.tools import REGISTRY
    assert "one window cannot be captured" in REGISTRY["screenshot"].openai_schema()["function"]["description"]


def test_sway_ipc(desk):
    desk["env"].setenv("WAYLAND_DISPLAY", "wayland-1")
    desk["env"].setenv("SWAYSOCK", "/run/sway.sock")
    desk["grim"]()
    tree = {"type": "root", "nodes": [{"type": "output", "nodes": [{"type": "workspace", "nodes": [
        {"type": "con", "pid": 1, "app_id": "foot", "name": "foot", "focused": True, "visible": True,
         "rect": {"x": 10, "y": 20, "width": 300, "height": 200}, "nodes": []}],
        "floating_nodes": [{"type": "floating_con", "pid": 2, "app_id": None, "name": "Gimp",
                            "window_properties": {"class": "Gimp"}, "visible": False,
                            "rect": {"x": 0, "y": 0, "width": 5, "height": 5}, "nodes": []}]}]}]}
    desk["tool"]("swaymsg", f"echo '{json.dumps(tree)}'")
    res, calls = desk["shot"]("window")
    assert calls[-1].startswith("grim -g 10,20 300x200 ")
    res, calls = desk["shot"]("gimp")
    assert "not on screen" in res["error"] and not calls


def test_hyprland_ipc(desk):
    desk["env"].setenv("WAYLAND_DISPLAY", "wayland-1")
    desk["env"].setenv("HYPRLAND_INSTANCE_SIGNATURE", "x")
    desk["grim"]()
    desk["tool"]("hyprctl", '''case "$1" in
  monitors) echo '[{"name":"DP-1","focused":true,"activeWorkspace":{"id":1}}]' ;;
  activewindow) echo '{"address":"0xb"}' ;;
  clients) echo '[{"address":"0xa","class":"kitty","title":"t","at":[0,0],"size":[10,10],"workspace":{"id":1}},
                  {"address":"0xb","class":"firefox","title":"f","at":[5,6],"size":[70,80],"workspace":{"id":1}}]' ;;
esac''')
    _, calls = desk["shot"]("window")
    assert calls[-1].startswith("grim -g 5,6 70x80 ")
    _, calls = desk["shot"]("screen")
    assert calls[-1].startswith("grim -o DP-1 ")


def test_x11(desk):
    desk["env"].setenv("DISPLAY", ":0")
    log, png = desk["log"], desk["bin"].parent / "shot.png"
    desk["tool"]("xdotool", f'''echo "xdotool $*" >> {log}; case "$1" in getactivewindow) echo 111 ;; search) echo 222 ;; esac''')
    desk["tool"]("import", f'''echo "import $*" >> {log}; for a; do last=$a; done; cp {png} "$last"''')
    _, calls = desk["shot"]("window")
    assert any(c.startswith("import -window 111 ") for c in calls)
    _, calls = desk["shot"]("Inkscape")
    assert "xdotool search --onlyvisible --name Inkscape" in calls and any("-window 222" in c for c in calls)
