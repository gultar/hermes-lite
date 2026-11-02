"""Python projects get their own virtualenv, automatically.

- Every terminal command runs with the project's `.venv` (or `venv`) active when one exists in the
  working folder or a parent: `python`, `pip` and installed tools point into it.
- The first `pip install` in a project without one creates `.venv` in the project folder first
  (`uv venv` if uv is installed, else `python -m venv`), so packages never land in the system Python.
- `--break-system-packages` is dropped when a venv is active (it does nothing there); without one it
  needs the user's approval (see terminal.DANGEROUS_PATTERNS).
- Projects managed by Poetry, Pipenv or Conda are left to their tool.
Switch off with agent.auto_venv: false."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PIP_INSTALL = re.compile(r"(^|[;&|(]\s*|\s)(sudo\s+)?(pip3?(\.\d+)?|python3?(\.\d+)?\s+-m\s+pip|uv\s+pip)\s+install\b")
BREAK = re.compile(r"\s--break-system-packages\b")
MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", ".git")


def bin_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def find_venv(start: Path) -> Path | None:
    """The nearest .venv/venv (a folder with pyvenv.cfg) in *start* or its parents, up to the home folder."""
    home = Path.home().resolve()
    for d in [start, *start.parents]:
        for name in (".venv", "venv"):
            if (d / name / "pyvenv.cfg").is_file():
                return d / name
        if d == home or d == d.parent:
            break
    return None


def other_manager(start: Path) -> str | None:
    """Poetry / Pipenv / Conda manage their own environments: leave those projects alone."""
    for d in [start, *start.parents]:
        if (d / "Pipfile").is_file():
            return "Pipenv"
        if (d / "environment.yml").is_file() or (d / "environment.yaml").is_file():
            return "Conda"
        py = d / "pyproject.toml"
        if py.is_file():
            try:
                if "[tool.poetry]" in py.read_text(encoding="utf-8", errors="replace"):
                    return "Poetry"
            except OSError:
                pass
            return None
        if d == Path.home() or d == d.parent:
            break
    return None


def project_root(start: Path) -> Path:
    """Where a new .venv goes: the nearest folder with a project marker, else the working folder."""
    home = Path.home().resolve()
    for d in [start, *start.parents]:
        if any((d / m).exists() for m in MARKERS):
            return d
        if d == home or d == d.parent:
            break
    return start


def create(root: Path) -> tuple[Path | None, str]:
    """Create root/.venv. Returns (venv or None, message)."""
    venv = root / ".venv"
    if shutil.which("uv"):
        argv = ["uv", "venv", "--quiet", "--seed", str(venv)]  # --seed: with pip inside, so `pip install` lands here
    elif os.name == "nt":
        argv = (["py", "-3", "-m", "venv", str(venv)] if shutil.which("py")
                else [shutil.which("python") or sys.executable, "-m", "venv", str(venv)])
    else:
        argv = [shutil.which("python3") or shutil.which("python") or sys.executable, "-m", "venv", str(venv)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"Could not create a virtualenv in {root} ({exc})."
    if r.returncode != 0 or not (venv / "pyvenv.cfg").is_file():
        return None, f"Could not create a virtualenv in {root}: {(r.stderr or r.stdout).strip()[-300:]}"
    return venv, (f"Created a virtualenv for this project at {venv} ({'uv' if argv[0] == 'uv' else 'python -m venv'}); "
                  "python and pip now use it in every command.")


LEADING_CD = re.compile(r"""^\s*cd\s+("[^"]+"|'[^']+'|[^\s;&|]+)\s*(&&|;)""")


def command_dir(command: str, start: Path) -> Path:
    """`cd /some/project && pip install …`: the project is the folder the command moves into."""
    m = LEADING_CD.match(command)
    if not m:
        return start
    target = Path(os.path.expanduser(m.group(1).strip("\"'")))
    target = target if target.is_absolute() else start / target
    return target if target.is_dir() else start


def will_have_venv(enabled: bool, command: str, start: Path) -> bool:
    """Whether this command will run inside a virtualenv (so --break-system-packages is harmless)."""
    if not enabled:
        return False
    start = command_dir(command, start)
    return find_venv(start) is not None or (bool(PIP_INSTALL.search(command)) and not re.search(r"\bsudo\s", command)
                                            and other_manager(start) is None)


def prepare(enabled: bool, command: str, start: Path) -> tuple[str, Path | None, str]:
    """(command to run, venv to activate or None, note for the tool result)."""
    start = command_dir(command, start)
    venv = find_venv(start) if enabled else None
    note = ""
    if enabled and venv is None and PIP_INSTALL.search(command) and not re.search(r"\bsudo\s", command):
        manager = other_manager(start)
        if manager:
            note = f"This project uses {manager}; Hermes Lite did not create a virtualenv. Use {manager} to install."
        else:
            venv, note = create(project_root(start))
    if venv is not None:
        command = BREAK.sub("", command)  # meaningless inside a venv
    return command, venv, note


def bash_lines(venv: Path | None) -> list[str]:
    """Activate *venv* for this command (and drop one Hermes Lite activated for another project).
    A virtualenv the model activated itself is left alone."""
    v = str(venv) if venv else ""
    b = str(bin_dir(venv)) if venv else ""
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"
    return [
        f"__hl_venv={q(v)}",
        'if [ -n "$__HL_AUTO_VENV" ] && [ "$__HL_AUTO_VENV" != "$__hl_venv" ]; then '
        'PATH=$(printf %s "$PATH" | tr ":" "\\n" | grep -vxF "$__HL_AUTO_VENV/bin" | paste -sd: -); '
        '[ "$VIRTUAL_ENV" = "$__HL_AUTO_VENV" ] && unset VIRTUAL_ENV; fi',
        'if [ -n "$__hl_venv" ] && { [ -z "$VIRTUAL_ENV" ] || [ "$VIRTUAL_ENV" = "$__HL_AUTO_VENV" ] || '
        '[ "$VIRTUAL_ENV" = "$__hl_venv" ]; }; then export VIRTUAL_ENV="$__hl_venv"; '
        f'case ":$PATH:" in *":"{q(b)}":"*) ;; *) export PATH={q(b)}":$PATH";; esac; unset PYTHONHOME; '
        'export __HL_AUTO_VENV="$__hl_venv"; else export __HL_AUTO_VENV=""; fi',
    ]


def powershell_lines(venv: Path | None) -> list[str]:
    v = str(venv).replace("'", "''") if venv else ""
    b = str(bin_dir(venv)).replace("'", "''") if venv else ""
    return [
        f"$__hl_venv = '{v}'; $__hl_bin = '{b}'; $__hl_sep = [IO.Path]::PathSeparator",
        "if ($env:__HL_AUTO_VENV -and $env:__HL_AUTO_VENV -ne $__hl_venv) {",
        "  $old = @((Join-Path $env:__HL_AUTO_VENV 'Scripts'), (Join-Path $env:__HL_AUTO_VENV 'bin'))",
        "  $env:PATH = (($env:PATH -split $__hl_sep) | Where-Object { $old -notcontains $_ }) -join $__hl_sep",
        "  if ($env:VIRTUAL_ENV -eq $env:__HL_AUTO_VENV) { Remove-Item env:VIRTUAL_ENV -ErrorAction SilentlyContinue } }",
        "if ($__hl_venv -and (-not $env:VIRTUAL_ENV -or $env:VIRTUAL_ENV -eq $env:__HL_AUTO_VENV -or $env:VIRTUAL_ENV -eq $__hl_venv)) {",
        "  $env:VIRTUAL_ENV = $__hl_venv",
        "  if (($env:PATH -split $__hl_sep) -notcontains $__hl_bin) { $env:PATH = $__hl_bin + $__hl_sep + $env:PATH }",
        "  $env:__HL_AUTO_VENV = $__hl_venv } else { $env:__HL_AUTO_VENV = '' }",
    ]
