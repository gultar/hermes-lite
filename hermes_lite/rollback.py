"""Rollback: earlier versions of Hermes Lite's code and of your config.yaml.

- Code: every ./install.sh saves a copy of the installed version (last 8 kept) in
  ~/.local/share/hermes-lite/versions/. `hermes-lite --rollback` puts the previous one back.
- Config: every launch saves config.yaml when it changed (last 15 kept) in
  ~/.hermes-lite/config-history/. `hermes-lite --rollback config` puts the previous one back.
Rolling back first saves what you have now, so a rollback can itself be undone the same way."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import home

PROJECT = Path(__file__).resolve().parent.parent          # editable install: the folder you unzipped
CODE_ITEMS = ("hermes_lite", "tests", "pyproject.toml", "install.sh", "install.ps1", "README.md")
KEEP_CODE, KEEP_CONFIG = 8, 15


def _versions_dir() -> Path:
    from .oscompat import data_dir
    d = data_dir() / "versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_dir() -> Path:
    d = home() / "config-history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for item in CODE_ITEMS:
        p = root / item
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file()
                                               and "__pycache__" not in f.parts) if p.is_dir() else []
        for f in files:
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _build(root: Path) -> str:
    try:
        for line in (root / "hermes_lite" / "__init__.py").read_text().splitlines():
            if line.startswith("BUILD"):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "unknown build"


def _code_versions() -> list[dict]:
    out = []
    for d in sorted(_versions_dir().iterdir()):
        meta = d / "version.json"
        if d.is_dir() and meta.is_file():
            try:
                out.append({**json.loads(meta.read_text()), "dir": d})
            except ValueError:
                pass
    return out


def snapshot_code(root: Path = PROJECT, quiet: bool = False) -> Path | None:
    """Save a copy of the code in *root* (skipped when the newest copy is identical)."""
    digest = _tree_hash(root)
    versions = _code_versions()
    if versions and versions[-1]["hash"] == digest:
        return versions[-1]["dir"]
    dest = _versions_dir() / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir()
    for item in CODE_ITEMS:
        src = root / item
        if src.is_dir():
            shutil.copytree(src, dest / item, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        elif src.is_file():
            shutil.copy2(src, dest / item)
    (dest / "version.json").write_text(json.dumps({"hash": digest, "build": _build(root),
                                                   "saved": time.strftime("%Y-%m-%d %H:%M")}))
    for old in _code_versions()[:-KEEP_CODE]:
        shutil.rmtree(old["dir"], ignore_errors=True)
    if not quiet:
        print(f"Saved this version for rollback: {_build(root)}")
    return dest


def rollback_code(latest: bool = False) -> int:
    """Previous version, or with latest=True the newest saved one (undoes a rollback)."""
    current = _tree_hash(PROJECT)
    versions = _code_versions()
    if latest:
        if not versions or versions[-1]["hash"] == current:
            print("Already on the newest saved version.")
            return 1
        older = [versions[-1]]
    else:
        idx = next((i for i, v in enumerate(versions) if v["hash"] == current), len(versions))
        older = [v for v in versions[:idx] if v["hash"] != current]
    if older:
        target, label = older[-1]["dir"], f"{older[-1]['build']} (installed {older[-1]['saved']})"
    else:
        bak = PROJECT.with_name(PROJECT.name + ".bak")
        if not (bak / "hermes_lite").is_dir():
            print("No earlier version saved yet (each ./install.sh saves one from now on).")
            return 1
        target, label = bak, f"{_build(bak)} (from {bak})"
    if not any(v["hash"] == current for v in versions):
        snapshot_code(quiet=True)  # keep what is replaced, so `--rollback latest` can bring it back
    for item in CODE_ITEMS:
        p = PROJECT / item
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()
        src = target / item
        if src.is_dir():
            shutil.copytree(src, p)
        elif src.is_file():
            shutil.copy2(src, p)
    # reinstall into the same venv so dependencies match that version
    if sys.prefix != sys.base_prefix:  # inside Hermes Lite's venv: match that version's dependencies
        if shutil.which("uv"):
            subprocess.run(["uv", "pip", "install", "--quiet", "-e", str(PROJECT)], check=False,
                           env={**os.environ, "VIRTUAL_ENV": sys.prefix})
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-e", str(PROJECT)], check=False)
    print(f"Rolled back the code to {label}. Restart Hermes Lite.")
    return 0


# ── config ───────────────────────────────────────────────────────────────
def _config_versions() -> list[Path]:
    return sorted(_config_dir().glob("config-*.yaml"))


def snapshot_config(path: Path) -> None:
    """Keep a copy of config.yaml whenever it changed since the last copy."""
    try:
        text = path.read_text()
    except OSError:
        return
    versions = _config_versions()
    if versions and versions[-1].read_text() == text:
        return
    (_config_dir() / f"config-{time.strftime('%Y%m%d-%H%M%S')}.yaml").write_text(text)
    for old in _config_versions()[:-KEEP_CONFIG]:
        old.unlink(missing_ok=True)


def rollback_config(path: Path) -> int:
    snapshot_config(path)
    current = path.read_text() if path.is_file() else ""
    older = [v for v in _config_versions() if v.read_text() != current]
    if not older:
        print("No earlier config saved yet (one is saved at each launch where config.yaml changed).")
        return 1
    target = older[-1]
    path.write_text(target.read_text())
    snapshot_config(path)
    (home() / "logs" / "server-tuning.json").unlink(missing_ok=True)  # re-check what fits
    stamp = target.stem.removeprefix("config-")
    print(f"Config restored to the version saved {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} "
          f"{stamp[9:11]}:{stamp[11:13]}. Restart Hermes Lite.")
    return 0


def list_versions(config_path: Path) -> int:
    current = _tree_hash(PROJECT)
    print("Code versions (newest last):")
    for v in _code_versions():
        print(f"  {v['saved']}  {v['build']}{'   ← running' if v['hash'] == current else ''}")
    print("\nConfig versions (newest last):")
    now = config_path.read_text() if config_path.is_file() else ""
    for v in _config_versions():
        print(f"  {v.stem.removeprefix('config-')}{'   ← current' if v.read_text() == now else ''}")
    print("\nhermes-lite --rollback          previous code version\n"
          "hermes-lite --rollback latest   newest saved code version (undo a rollback)\n"
          "hermes-lite --rollback config   previous config.yaml")
    return 0
