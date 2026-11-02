"""screenshot and view_image: let a vision-capable model look at a window, the screen, a web page or
an image file. Only offered when llama-server has a vision encoder (--mmproj) loaded.

The image itself is not put in the tool result. The agent attaches it once, in a message right after
the tool results; after the model's next reply it is replaced by a text note of what the model saw,
so images never pile up in the context."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .base import Tool, ToolContext, error, ok, register, short

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _image_dir(ctx: ToolContext) -> Path:
    d = ctx.home / "scratch" / ctx.session_id / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run with a hard timeout; on timeout the whole process group (browser helpers too) is killed."""
    from .. import oscompat
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **oscompat.new_group_kwargs())
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        oscompat.kill_tree(proc.pid, grace=0.1)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(argv, -9, out or "", (err or "") + f"\n(timed out after {timeout}s)")
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def prepare(src: Path, ctx: ToolContext) -> tuple[Path, str]:
    """Scale the image down (long side <= agent.image_max_side) and save it as JPEG for the model.
    Returns (path, "WxH")."""
    max_side = int(ctx.cfg.get_path("agent.image_max_side", 1280))
    dest = _image_dir(ctx) / f"img-{int(time.time() * 1000)}.jpg"
    try:
        from PIL import Image
    except ImportError:
        # Without Pillow: send it as is (llama-server reads png/jpg itself)
        out = dest.with_suffix(src.suffix.lower() or ".png")
        shutil.copyfile(src, out)
        return out, "original size"
    with Image.open(src) as im:
        im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        im.save(dest, "JPEG", quality=85)
        return dest, f"{im.width}x{im.height}"


def _attach(ctx: ToolContext, path: Path, label: str, question: str) -> None:
    ctx.state.setdefault("pending_images", []).append({"path": str(path), "label": label, "question": question})


# ── screen capture ───────────────────────────────────────────────────────
def _hypr(*args: str):
    r = _run(["hyprctl", *args, "-j"], 10)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def _visible_workspaces() -> set[int]:
    mons = _hypr("monitors") or []
    ids = {m.get("activeWorkspace", {}).get("id") for m in mons}
    ids |= {m.get("specialWorkspace", {}).get("id") for m in mons if m.get("specialWorkspace", {}).get("id")}
    return ids


def _capture_wayland(target: str, raw: Path) -> str | None:
    """grim (+ hyprctl for windows on Hyprland). Returns an error message or None."""
    if not shutil.which("grim"):
        return "grim is not installed (sudo pacman -S grim)."
    geom = None
    t = target.strip().lower()
    hypr = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl")
    if t in ("window", "active", "active window", "focused"):
        if not hypr:
            return "Capturing the active window needs Hyprland; use target='screen'."
        w = _hypr("activewindow")
        if not w or not w.get("size"):
            return "No active window."
        geom = w
    elif t not in ("screen", "monitor", "all", "desktop"):
        if not hypr:
            return "Capturing a window by name needs Hyprland; use target='screen'."
        clients = _hypr("clients") or []
        match = [c for c in clients if t in (c.get("class", "") + " " + c.get("title", "")).lower()]
        if not match:
            names = "; ".join(short(f"{c.get('class')}: {c.get('title')}", 60) for c in clients[:15])
            return f"No window matches {target!r}. Open windows: {names}"
        visible = _visible_workspaces()
        shown = [c for c in match if c.get("workspace", {}).get("id") in visible]
        if not shown:
            ws = match[0].get("workspace", {}).get("name")
            return (f"The window {match[0].get('title')!r} is on workspace {ws}, which is not on screen, so it "
                    "cannot be captured. Ask the user to bring it on screen (it is not moved for them).")
        geom = shown[0]
    if geom:
        (x, y), (w, h) = geom["at"], geom["size"]
        argv = ["grim", "-g", f"{x},{y} {w}x{h}", str(raw)]
    elif hypr:
        mon = next((m for m in (_hypr("monitors") or []) if m.get("focused")), None)
        argv = ["grim", "-o", mon["name"], str(raw)] if mon else ["grim", str(raw)]
    else:
        argv = ["grim", str(raw)]
    r = _run(argv)
    return None if r.returncode == 0 and raw.exists() else f"grim failed: {(r.stderr or r.stdout).strip()[:300]}"


def _capture_x11(target: str, raw: Path) -> str | None:
    t = target.strip().lower()
    if t in ("window", "active", "focused") and shutil.which("import") and shutil.which("xdotool"):
        wid = _run(["xdotool", "getactivewindow"]).stdout.strip()
        r = _run(["import", "-window", wid, str(raw)])
    elif shutil.which("maim"):
        r = _run(["maim", str(raw)])
    elif shutil.which("scrot"):
        r = _run(["scrot", "-o", str(raw)])
    elif shutil.which("import"):
        r = _run(["import", "-window", "root", str(raw)])
    else:
        return "No screenshot program found (install maim, scrot or imagemagick)."
    return None if r.returncode == 0 and raw.exists() else f"capture failed: {(r.stderr or r.stdout).strip()[:300]}"


_MAC_APPS = {"chromium": "/Applications/Chromium.app/Contents/MacOS/Chromium",
             "google-chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
             "microsoft-edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"}


def _win_paths(*rel: str) -> list[str]:
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    return [os.path.join(r, x) for r in roots if r for x in rel]


def _browsers() -> list[tuple[str, str]]:
    """(name, executable) for Chromium-family browsers on this machine, most suitable first."""
    found: list[tuple[str, str]] = []
    for exe in ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
                "brave", "brave-browser", "vivaldi-stable", "microsoft-edge-stable", "chrome", "msedge"):
        if path := shutil.which(exe):
            found.append((exe, path))
    if sys.platform == "darwin":
        found += [(n, p) for n, p in _MAC_APPS.items() if os.path.exists(p)]
    if os.name == "nt":  # Edge ships with Windows
        for name, rel in (("google-chrome", r"Google\Chrome\Application\chrome.exe"),
                          ("brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
                          ("msedge", r"Microsoft\Edge\Application\msedge.exe")):
            found += [(name, p) for p in _win_paths(rel) if os.path.exists(p)]
    seen, out = set(), []
    for name, path in found:
        if path not in seen:
            seen.add(path)
            out.append((name, path))
    return out


def _firefox() -> str | None:
    if path := shutil.which("firefox"):
        return path
    for p in ["/Applications/Firefox.app/Contents/MacOS/firefox", *_win_paths(r"Mozilla Firefox\firefox.exe")]:
        if os.path.exists(p):
            return p
    return None


def _capture_windows(raw: Path) -> str | None:
    """The whole virtual screen, through .NET (PowerShell)."""
    from .. import oscompat
    ps = oscompat.powershell()
    if not ps:
        return "PowerShell not found."
    script = (
        "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
        "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
        f"$bmp.Save('{str(raw).replace(chr(39), chr(39) * 2)}', [System.Drawing.Imaging.ImageFormat]::Png)")
    r = _run([ps, "-NoProfile", "-NonInteractive", "-Command", script], 30)
    return None if raw.exists() else f"Screen capture failed: {(r.stderr or r.stdout).strip()[:300]}"


def _capture_mac(raw: Path) -> str | None:
    r = _run(["screencapture", "-x", str(raw)], 30)
    if raw.exists():
        return None
    return ("Screen capture failed (macOS needs Screen Recording permission for your terminal app: System "
            f"Settings → Privacy & Security). {(r.stderr or '').strip()[:200]}")


def _capture_url(url: str, raw: Path, width: int, height: int, wait_ms: int) -> str | None:
    """Render a web page in a headless browser with a throwaway profile (never the user's own).
    Tries chrome-headless-shell, then Chromium-family browsers, then Firefox."""
    tried = []
    root = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    for exe, path in _browsers():
        with tempfile.TemporaryDirectory() as prof:
            head = [] if exe == "chrome-headless-shell" else ["--headless=new", "--no-first-run"]
            r = _run([path, *head, "--disable-gpu", "--hide-scrollbars", *root, f"--user-data-dir={prof}",
                      f"--window-size={width},{height}", f"--virtual-time-budget={wait_ms}",
                      f"--screenshot={raw}", url], 45)
        if raw.exists() and raw.stat().st_size:
            return None
        tried.append(f"{exe}: {(r.stderr or '').strip().splitlines()[-1:] or ['no output']}")
    if path := _firefox():
        with tempfile.TemporaryDirectory() as prof:
            r = _run([path, "--headless", "--no-remote", "--profile", prof, "--window-size", f"{width},{height}",
                      "--screenshot", str(raw), url], 60)
        if raw.exists() and raw.stat().st_size:
            return None
        tried.append(f"firefox: {(r.stderr or '').strip().splitlines()[-1:] or ['no output']}")
    if not tried:
        return "No Chromium-family browser or Firefox found to render the page."
    return "Could not render the page. " + " | ".join(tried)


def screenshot(args: dict, ctx: ToolContext) -> str:
    target = str(args.get("target") or "screen").strip()
    question = str(args.get("look_for") or "").strip()
    raw = _image_dir(ctx) / f"raw-{int(time.time() * 1000)}.png"
    if target.startswith(("http://", "https://", "file://")) or target.startswith("localhost"):
        url = target if "://" in target else "http://" + target
        err = _capture_url(url, raw, int(args.get("width") or 1280), int(args.get("height") or 800),
                           int(args.get("wait_ms") or 2500))
        label = f"web page {url}"
    elif os.name == "nt" or sys.platform == "darwin":
        if target.lower() not in ("screen", "monitor", "all", "desktop", "window", "active", "focused"):
            return error("On this system only the whole screen (target='screen') or a web page URL can be captured.")
        err = _capture_windows(raw) if os.name == "nt" else _capture_mac(raw)
        label = "the screen"
    elif os.environ.get("WAYLAND_DISPLAY"):
        err = _capture_wayland(target, raw)
        label = "the active window" if target.lower() in ("window", "active", "focused") else (
            "the screen" if target.lower() in ("screen", "monitor", "all", "desktop") else f"window {target!r}")
    elif os.environ.get("DISPLAY"):
        err = _capture_x11(target, raw)
        label = "the active window" if target.lower() in ("window", "active", "focused") else "the screen"
    else:
        err = "No graphical session (neither WAYLAND_DISPLAY nor DISPLAY is set)."
        label = ""
    if err:
        return error(err)
    try:
        path, size = prepare(raw, ctx)
    finally:
        raw.unlink(missing_ok=True)
    _attach(ctx, path, f"screenshot of {label}", question)
    return ok(success=True, image=str(path), size=size, shows=label,
              note="The screenshot is attached right after these tool results. You see it ONCE: in your next "
                   "reply, write down everything in it that matters for the task (text, values, layout, errors) "
                   "before anything else — afterwards it is replaced by those notes.")


def view_image(args: dict, ctx: ToolContext) -> str:
    raw = str(args.get("path") or "").strip()
    if not raw:
        return error("view_image: missing required field 'path'.")
    src = ctx.resolve(raw)
    if not src.is_file():
        return error(f"File not found: {src}")
    if src.suffix.lower() not in IMAGE_EXT:
        return error(f"{src.name} is not an image this tool can show ({', '.join(sorted(IMAGE_EXT))}). "
                     "For an SVG, read it as text with read_file.")
    try:
        path, size = prepare(src, ctx)
    except Exception as exc:  # unreadable or corrupt image
        return error(f"Could not open {src} as an image: {exc}")
    _attach(ctx, path, f"image {src.name}", str(args.get("look_for") or "").strip())
    return ok(success=True, image=str(src), size=size,
              note="The image is attached right after these tool results. You see it ONCE: in your next reply, "
                   "write down everything in it that matters for the task before anything else — afterwards it "
                   "is replaced by those notes.")


LOOK = {"type": "string", "description": "What to check in the image (e.g. 'is the login form visible, any error text?')."}

register(Tool(
    name="screenshot", emoji="📸", requires="vision",
    schema={"name": "screenshot", "description": (
        "Take a screenshot and look at it: the screen the user is on (default), a window by name (its title or "
        "app class, e.g. 'firefox', 'Strategy Game'), or a web page rendered headless from a URL "
        "(http://localhost:3000 — best for checking a web app you are building: no need for the user's "
        "browser). The image is shown to you once, after this call; note what you see in your next reply. "
        "Only the user's visible screen can be captured — windows on hidden workspaces cannot."),
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "'screen', a window name, or a URL. ('window' = the "
                       "focused window, usually the terminal you run in.)", "default": "screen"},
            "look_for": LOOK,
            "width": {"type": "integer", "description": "Viewport width for a URL (default 1280)."},
            "height": {"type": "integer", "description": "Viewport height for a URL (default 800)."},
            "wait_ms": {"type": "integer", "description": "For a URL: let scripts run this long first (default 2500)."},
        }, "required": []}},
    handler=screenshot, label=lambda a: short(a.get("target") or "screen", 70),
))

register(Tool(
    name="view_image", emoji="🖼", read_only=True, requires="vision",
    schema={"name": "view_image", "description": (
        "Look at an image file (png, jpg, webp, gif, bmp): a screenshot the user saved, a mockup, a chart a "
        "script produced, a test's screenshot. Shown to you once, after this call; note what you see in your "
        "next reply."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Image file path."},
            "look_for": LOOK,
        }, "required": ["path"]}},
    handler=view_image, label=lambda a: short(a.get("path", ""), 70),
))
