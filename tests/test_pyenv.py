"""Automatic project virtualenvs."""
import json
import os
import shutil

import pytest

from hermes_lite import pyenv
from hermes_lite.config import load_config
from hermes_lite.tools.base import ToolContext
from hermes_lite.tools.terminal import terminal


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "requirements.txt").write_text("")
    (tmp_path / "other").mkdir()
    return tmp_path


def _ctx(proj, cwd, asked=None, **cfg_agent):
    cfg = load_config()
    cfg["agent"].update(cfg_agent)
    return ToolContext(cfg=cfg, home=proj / "hl", cwd=str(proj / cwd), session_id="t",
                       approve=lambda c, r: (asked.append(r) if asked is not None else None) or "deny")


@pytest.mark.skipif(not shutil.which("bash"), reason="bash")
def test_first_pip_install_creates_and_activates_a_project_venv(proj, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")              # without uv: python -m venv
    asked = []
    ctx = _ctx(proj, "app", asked)
    run = lambda c: json.loads(terminal({"command": c, "timeout": 170}, ctx))
    r = run("pip install --break-system-packages --help >/dev/null")
    assert (proj / "app" / ".venv" / "pyvenv.cfg").is_file() and "Created a virtualenv" in r["note"] and not asked
    assert run("python3 -c 'import sys; print(sys.prefix)'")["output"].strip() == str(proj / "app" / ".venv")
    ctx.cwd = str(proj / "other")                             # another project: that venv is not active there
    assert run("python3 -c 'import sys; print(sys.prefix)'")["output"].strip() != str(proj / "app" / ".venv")


def test_poetry_projects_and_system_installs_are_left_alone(proj):
    (proj / "other" / "pyproject.toml").write_text("[tool.poetry]\nname='x'\n")
    cmd, venv, note = pyenv.prepare(True, "pip install requests", proj / "other")
    assert venv is None and "Poetry" in note and not (proj / "other" / ".venv").exists()
    asked = []
    r = json.loads(terminal({"command": "pip install --break-system-packages requests"}, _ctx(proj, "other", asked)))
    assert asked and "system Python" in asked[0] and r.get("exit_code") == -1   # asked, and denied here
    assert pyenv.prepare(False, "pip install requests", proj / "app")[1] is None     # auto_venv: false


def test_leading_cd_picks_the_project():
    from pathlib import Path
    assert pyenv.command_dir("cd /tmp && pip install x", Path("/")) == Path("/tmp")
    assert pyenv.command_dir("pip install x", Path("/usr")) == Path("/usr")
