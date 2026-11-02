"""System clipboard: wl-clipboard on Wayland, xclip/xsel on X11. Every copy is verified by reading
the clipboard back, so the UI can say truthfully whether it worked."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess

COPY_TOOLS = (
    ("wl-copy", ["wl-copy"], ["wl-paste", "--no-newline"]),
    ("xclip", ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]),
    ("xsel", ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]),
    ("pbcopy", ["pbcopy"], ["pbpaste"]),                                   # macOS
)


def _tools() -> tuple:
    if os.name == "nt":  # Windows: PowerShell's clipboard cmdlets (clip.exe mangles Unicode)
        ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return (("powershell", [ps, "-NoProfile", "-Command",
                                "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                 [ps, "-NoProfile", "-Command",
                  "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"]),)
    return COPY_TOOLS


def _env() -> dict:
    env = dict(os.environ)
    if not env.get("WAYLAND_DISPLAY") and hasattr(os, "getuid"):
        # Started from a shell that lost the variable: find the compositor's socket ourselves.
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        sockets = sorted(p for p in glob.glob(os.path.join(runtime, "wayland-*")) if not p.endswith(".lock"))
        if sockets:
            env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
            env.setdefault("XDG_RUNTIME_DIR", runtime)
    return env


def _read(argv: list[str], env: dict) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=3, env=env)
        return r.stdout.decode(errors="replace") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def copy(text: str) -> tuple[str | None, str]:
    """Returns (tool that verifiably holds the text, or None; a short reason when it failed)."""
    env = _env()
    reasons = []
    for name, copy_argv, paste_argv in _tools():
        if not shutil.which(copy_argv[0]):
            continue
        try:
            subprocess.run(copy_argv, input=text.encode(), timeout=3, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"{name}: {exc}")
            continue
        back = _read(paste_argv, env) if shutil.which(paste_argv[0]) else None
        if back is not None and back.replace("\r\n", "\n").rstrip("\n") == text.replace("\r\n", "\n").rstrip("\n"):
            return name, ""
        reasons.append(f"{name} did not keep the text")
    if not reasons:
        reasons.append("no clipboard tool installed (wl-clipboard on Wayland, xclip on X11)")
    return None, "; ".join(reasons)


def paste() -> str | None:
    env = _env()
    for _, _, paste_argv in _tools():
        if shutil.which(paste_argv[0]):
            text = _read(paste_argv, env)
            if text is not None:
                return text
    return None
