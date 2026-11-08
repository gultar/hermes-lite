"""Configuration: packaged defaults merged with ~/.hermes-lite/config.yaml."""

from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(__file__).with_name("default_config.yaml")

STARTER_CONFIG = """\
# Hermes Lite — your settings. Only what you put here overrides the defaults.
# Every option, with comments: hermes_lite/default_config.yaml in the install folder.
# Check the resulting server command with: hermes-lite --print-server-cmd

server:
  model_path: ~/models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf
  binary: llama-server
  n_cpu_moe: 38
  terminal: auto           # "none" = no server window (log: /server, or ~/.hermes-lite/logs/llama-server.log)
  extra_args: []
"""


def home() -> Path:
    """Hermes Lite's home: config, memories, skills, sessions, logs, scratch."""
    path = Path(os.environ.get("HERMES_LITE_HOME", "~/.hermes-lite")).expanduser()
    for sub in ("memories", "skills", "sessions", "logs", "scratch"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(dict):
    """Nested dict with dotted access: cfg.get_path("server.port")."""

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_config(path: str | None = None) -> Config:
    defaults = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
    user_path = Path(path).expanduser() if path else home() / "config.yaml"
    if not path and not user_path.exists():
        # First run: a short override file. A full copy of the defaults would silently shadow
        # every default changed in later versions.
        user_path.write_text(STARTER_CONFIG, encoding="utf-8")
    user = {}
    if user_path.exists():
        user = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
    cfg = Config(_merge(defaults, user))
    srv_user = user.get("server") if isinstance(user, dict) else None
    if isinstance(srv_user, dict) and "mmproj_offload" in srv_user and "mmproj_device" not in srv_user:
        cfg["server"]["mmproj_device"] = "vram" if srv_user["mmproj_offload"] else "ram"  # older setting
    cfg["_path"] = str(user_path)
    model, server = cfg.setdefault("model", {}), cfg.setdefault("server", {})
    if str(model.get("base_url") or "auto").strip().lower() == "auto":
        model["base_url"] = f"http://{server.get('host', '127.0.0.1')}:{server.get('port', 8091)}/v1"
    return cfg


def set_value(path: Path, dotted: str, value: str) -> str:
    """Set section.key in config.yaml, editing only that line (comments and the rest stay as
    they are). Returns what was done."""
    import re
    section, _, key = dotted.partition(".")
    if not section or not key or "." in key:
        raise ValueError("use section.key=value, e.g. server.port=8080")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    head = next((i for i, l in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", l)), None)
    entry = f"  {key}: {value}"
    if head is None:
        lines += ([""] if lines and lines[-1].strip() else []) + [f"{section}:", entry]
        done = f"added {dotted}: {value}"
    else:
        end = next((i for i in range(head + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))),
                   len(lines))
        found = next((i for i in range(head + 1, end) if re.match(rf"^\s+{re.escape(key)}:", lines[i])), None)
        if found is None:
            lines.insert(head + 1, entry)
            done = f"added {dotted}: {value}"
        else:
            indent = re.match(r"^(\s+)", lines[found]).group(1)
            lines[found] = f"{indent}{key}: {value}"
            done = f"set {dotted}: {value}"
    new = "\n".join(lines) + "\n"
    yaml.safe_load(new)  # never write a file that no longer parses
    path.write_text(new, encoding="utf-8")
    return done
