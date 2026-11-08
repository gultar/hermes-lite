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
SCREEN_WORDS = ("screen", "monitor", "all", "desktop")
ACTIVE_WORDS = ("window", "active", "active window", "focused")


class NoWindowCapture(Exception):
    """This desktop offers no way to capture one window: the whole screen is captured instead."""


def _json(argv: list[str]):
    r = _run(argv, 10)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _pick(windows: list[dict], target: str) -> dict:
    """The focused window ("window"), or the first whose title or app id contains *target*."""
    t = target.strip().lower()
    if t in ACTIVE_WORDS:
        hit = next((w for w in windows if w.get("focused")), None)
        if not hit:
            raise NoWindowCapture("this desktop does not say which window is focused")
        return hit
    hit = next((w for w in windows if t in f"{w.get('app', '')} {w.get('title', '')}".lower()), None)
    if not hit:
        names = "; ".join(short(f"{w.get('app')}: {w.get('title')}", 60) for w in windows[:15])
        raise LookupError(f"No window matches {target!r}. Open windows: {names or '(none listed)'}")
    return hit


# Window lists, each from what the running desktop actually offers (no setting names a desktop):
# Hyprland's and Sway's IPC give positions (then grim crops the screen); any compositor with the
# ext-foreign-toplevel-list + ext-image-copy-capture protocols (MangoWM, niri, recent wlroots ones)
# lets grim (1.5+) capture a window directly, listed by lswt.
def _hyprland_windows() -> list[dict] | None:
    if not (os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl")):
        return None
    mons = _json(["hyprctl", "monitors", "-j"]) or []
    shown = {m.get("activeWorkspace", {}).get("id") for m in mons} | \
            {m.get("specialWorkspace", {}).get("id") for m in mons if m.get("specialWorkspace", {}).get("id")}
    active = (_json(["hyprctl", "activewindow", "-j"]) or {}).get("address")
    return [{"app": c.get("class", ""), "title": c.get("title", ""), "focused": c.get("address") == active,
             "visible": c.get("workspace", {}).get("id") in shown, "geom": (*c["at"], *c["size"])}
            for c in _json(["hyprctl", "clients", "-j"]) or [] if c.get("at") and c.get("size")]


def _sway_windows() -> list[dict] | None:
    if not (os.environ.get("SWAYSOCK") and shutil.which("swaymsg")):
        return None
    out: list[dict] = []

    def walk(node: dict) -> None:
        kids = node.get("nodes", []) + node.get("floating_nodes", [])
        if not kids and node.get("type") in ("con", "floating_con") and node.get("pid"):
            r = node.get("rect", {})
            out.append({"app": node.get("app_id") or (node.get("window_properties") or {}).get("class", ""),
                        "title": node.get("name") or "", "focused": bool(node.get("focused")),
                        "visible": node.get("visible", True),
                        "geom": (r.get("x", 0), r.get("y", 0), r.get("width", 0), r.get("height", 0))})
        for k in kids:
            walk(k)
    walk(_json(["swaymsg", "-t", "get_tree"]) or {})
    return out


def _lswt(*extra: str) -> list[dict]:
    data = _json(["lswt", "-j", *extra])
    items = data.get("toplevels") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if isinstance(it, dict):
            k = {str(key).lower().replace("-", "").replace("_", ""): v for key, v in it.items()}
            out.append({"app": str(k.get("appid") or ""), "title": str(k.get("title") or ""),
                        "id": str(k.get("identifier") or ""), "activated": k.get("activated")})
    return out


def _toplevel_windows() -> list[dict] | None:
    """Windows grim can capture by identifier: ext-foreign-toplevel-list ids (lswt), focus from the wlr
    protocol where the compositor has it. None when grim or lswt cannot do it here."""
    if not (shutil.which("lswt") and shutil.which("grim")):
        return None
    r = _run(["grim", "-h"], 10)
    if "-T" not in (r.stdout + r.stderr):     # grim older than 1.5
        return None
    tops = _lswt()
    if not any(t["id"] for t in tops):
        tops = _lswt("--force-protocol", "ext-foreign-toplevel-list-v1")
    if not any(t["id"] for t in tops):
        return None
    if all(t["activated"] is None for t in tops):   # ids came without focus: ask the wlr protocol for it
        focus = {(t["app"], t["title"]) for t in
                 _lswt("--force-protocol", "wlr-foreign-toplevel-management-unstable-v1")
                 if t["activated"] is True or str(t["activated"]).lower() in ("true", "1", "yes")}
        for t in tops:
            t["activated"] = (t["app"], t["title"]) in focus
    return [{"app": t["app"], "title": t["title"], "focused": bool(t["activated"] is True or
             str(t["activated"]).lower() in ("true", "1", "yes")), "visible": True, "toplevel": t["id"]}
            for t in tops if t["id"]]


def _capture_window_wayland(target: str, raw: Path) -> str:
    """Capture one window; returns its label. LookupError: no such window; NoWindowCapture: not
    possible on this desktop."""
    if not shutil.which("grim"):
        raise NoWindowCapture("grim is not installed")
    for source in (_hyprland_windows, _sway_windows, _toplevel_windows):
        windows = source()
        if windows is None:
            continue
        w = _pick(windows, target)
        if not w.get("visible", True):
            raise LookupError(f"The window {w.get('title')!r} is on a workspace that is not on screen, so it "
                              "cannot be captured. Ask the user to bring it on screen (it is not moved for them).")
        argv = (["grim", "-T", w["toplevel"], str(raw)] if w.get("toplevel") else
                ["grim", "-g", "{},{} {}x{}".format(*w["geom"]), str(raw)])
        r = _run(argv)
        if r.returncode != 0 or not raw.exists():
            raise NoWindowCapture(f"grim could not capture it ({(r.stderr or r.stdout).strip()[:200]})")
        return f"window {short(w.get('title') or w.get('app') or target, 60)!r}"
    raise NoWindowCapture("this desktop gives no window list (for MangoWM, niri and other wlroots-style "
                          "compositors: install lswt and grim 1.5 or newer)")


def _capture_screen_wayland(raw: Path) -> str | None:
    if not shutil.which("grim"):
        return "grim is not installed (sudo pacman -S grim)."
    argv = ["grim", str(raw)]
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):   # just the focused monitor
        mon = next((m for m in (_json(["hyprctl", "monitors", "-j"]) or []) if m.get("focused")), None)
        argv = ["grim", "-o", mon["name"], str(raw)] if mon else argv
    elif os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        mon = next((o for o in (_json(["swaymsg", "-t", "get_outputs"]) or []) if o.get("focused")), None)
        argv = ["grim", "-o", mon["name"], str(raw)] if mon else argv
    r = _run(argv)
    return None if r.returncode == 0 and raw.exists() else f"grim failed: {(r.stderr or r.stdout).strip()[:300]}"


def _capture_window_x11(target: str, raw: Path) -> str:
    if not shutil.which("xdotool") or not (shutil.which("import") or shutil.which("maim")):
        raise NoWindowCapture("xdotool and imagemagick (or maim) are needed for one window")
    t = target.strip()
    if t.lower() in ACTIVE_WORDS:
        wid = _run(["xdotool", "getactivewindow"]).stdout.strip()
    else:
        wid = (_run(["xdotool", "search", "--onlyvisible", "--name", t]).stdout.split() or [""])[0]
        if not wid:
            raise LookupError(f"No visible window matches {target!r}.")
    r = _run(["import", "-window", wid, str(raw)] if shutil.which("import") else ["maim", "-i", wid, str(raw)])
    if r.returncode != 0 or not raw.exists():
        raise NoWindowCapture(f"capture failed: {(r.stderr or r.stdout).strip()[:200]}")
    return "the active window" if t.lower() in ACTIVE_WORDS else f"window {t!r}"


def _capture_screen_x11(raw: Path) -> str | None:
    if shutil.which("maim"):
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


def _capture_windows(raw: Path, target: str = "screen") -> str | None:
    """The whole virtual screen, or one window (the foreground one, or by title or process name),
    through .NET (PowerShell)."""
    from .. import oscompat
    ps = oscompat.powershell()
    if not ps:
        return "PowerShell not found."
    q = lambda v: "'" + str(v).replace("'", "''") + "'"
    t = target.strip()
    if t.lower() in SCREEN_WORDS:
        grab = ("$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
                "$x=$b.Left; $y=$b.Top; $w=$b.Width; $h=$b.Height; ")
    else:
        find = ("$hw = [W]::GetForegroundWindow(); " if t.lower() in ACTIVE_WORDS else
                f"$p = Get-Process | Where-Object {{ $_.MainWindowHandle -ne 0 -and ($_.MainWindowTitle -like "
                f"('*' + {q(t)} + '*') -or $_.ProcessName -like ('*' + {q(t)} + '*')) }} | Select-Object -First 1; "
                "if (-not $p) { Write-Error 'NOMATCH'; exit 3 }; $hw = $p.MainWindowHandle; "
                "[W]::ShowWindow($hw, 9) | Out-Null; [W]::SetForegroundWindow($hw) | Out-Null; Start-Sleep -Milliseconds 300; ")
        grab = ("Add-Type @'\nusing System; using System.Runtime.InteropServices;\npublic class W {\n"
                "[DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();\n"
                "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);\n"
                "[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int c);\n"
                "[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();\n"
                "[DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr h, out RECT r);\n"
                "public struct RECT { public int Left, Top, Right, Bottom; } }\n'@\n"
                "[W]::SetProcessDPIAware() | Out-Null; " + find +
                "$r = New-Object W+RECT; [W]::GetWindowRect($hw, [ref]$r) | Out-Null; "
                "$x=$r.Left; $y=$r.Top; $w=$r.Right-$r.Left; $h=$r.Bottom-$r.Top; ")
    script = ("Add-Type -AssemblyName System.Windows.Forms, System.Drawing; " + grab +
              "$bmp = New-Object System.Drawing.Bitmap $w, $h; "
              "$g = [System.Drawing.Graphics]::FromImage($bmp); "
              "$g.CopyFromScreen($x, $y, 0, 0, $bmp.Size); "
              f"$bmp.Save({q(raw)}, [System.Drawing.Imaging.ImageFormat]::Png)")
    r = _run([ps, "-NoProfile", "-NonInteractive", "-Command", script], 30)
    if "NOMATCH" in (r.stderr or ""):
        raise LookupError(f"No window matches {target!r} (title or program name).")
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
        note = ""
        url = target if "://" in target else "http://" + target
        err = _capture_url(url, raw, int(args.get("width") or 1280), int(args.get("height") or 800),
                           int(args.get("wait_ms") or 2500))
        label = f"web page {url}"
    else:
        err, label, note = None, "the screen", ""
        t = target.lower()
        try:
            if t in SCREEN_WORDS:
                raise NoWindowCapture("")
            if os.name == "nt":
                err = _capture_windows(raw, target)
                label = "the active window" if t in ACTIVE_WORDS else f"window {target!r}"
            elif sys.platform == "darwin":
                raise NoWindowCapture("macOS window capture is not supported yet")
            elif os.environ.get("WAYLAND_DISPLAY"):
                label = _capture_window_wayland(target, raw)
            elif os.environ.get("DISPLAY"):
                label = _capture_window_x11(target, raw)
            else:
                err = "No graphical session (neither WAYLAND_DISPLAY nor DISPLAY is set)."
        except LookupError as exc:
            return error(str(exc))
        except NoWindowCapture as exc:   # the whole screen instead, and say why
            note = (f"One window could not be captured here ({exc}): this is the whole screen. "
                    if str(exc) else "")
            label = "the screen"
            if os.name == "nt":
                err = _capture_windows(raw)
            elif sys.platform == "darwin":
                err = _capture_mac(raw)
            elif os.environ.get("WAYLAND_DISPLAY"):
                err = _capture_screen_wayland(raw)
            elif os.environ.get("DISPLAY"):
                err = _capture_screen_x11(raw)
            else:
                err = "No graphical session (neither WAYLAND_DISPLAY nor DISPLAY is set)."
    if err:
        return error(err)
    try:
        path, size = prepare(raw, ctx)
    finally:
        raw.unlink(missing_ok=True)
    _attach(ctx, path, f"screenshot of {label}", question)
    return ok(success=True, image=str(path), size=size, shows=label,
              note=note + "The screenshot is attached right after these tool results. You see it ONCE: in your next "
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


def window_capture_available() -> bool:
    """Can one window be captured here? (Checked from what is installed and running, not a setting.)"""
    if os.name == "nt":
        return True
    if sys.platform == "darwin":
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return bool(shutil.which("grim")) and bool(
            (os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"))
            or (os.environ.get("SWAYSOCK") and shutil.which("swaymsg")) or shutil.which("lswt"))
    return bool(os.environ.get("DISPLAY") and shutil.which("xdotool") and (shutil.which("import") or shutil.which("maim")))


def _window_note() -> str:
    """Added to the screenshot description (after prompts/tool_screenshot.md): what this desktop allows."""
    if window_capture_available():
        return ("It can also capture one window by name (its title or app name, e.g. 'firefox'), or 'window' "
                "for the focused one; only windows on screen, not on hidden workspaces.")
    return "On this desktop one window cannot be captured: use 'screen' or a URL."


def _screenshot_description() -> str:
    return ("Take a screenshot and look at it: the screen the user is on (default), or a web page rendered "
            "headless from a URL (http://localhost:3000 — best for checking a web app you are building: no need "
            "for the user's browser). The image is shown to you once, after this call; note what you see in your "
            "next reply.")


LOOK = {"type": "string", "description": "What to check in the image (e.g. 'is the login form visible, any error text?')."}

register(Tool(
    name="screenshot", emoji="📸", requires="vision",
    schema={"name": "screenshot", "description": _screenshot_description(),
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "'screen', a window name, or a URL. ('window' = the "
                       "focused window, usually the terminal you run in.)", "default": "screen"},
            "look_for": LOOK,
            "width": {"type": "integer", "description": "Viewport width for a URL (default 1280)."},
            "height": {"type": "integer", "description": "Viewport height for a URL (default 800)."},
            "wait_ms": {"type": "integer", "description": "For a URL: let scripts run this long first (default 2500)."},
        }, "required": []}},
    handler=screenshot, label=lambda a: short(a.get("target") or "screen", 70), extra=_window_note,
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
