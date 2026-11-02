"""Offline parts of the research tools: section filtering, README cleanup, project version pins."""
import threading
from pathlib import Path

from hermes_lite.config import Config
from hermes_lite.tools.base import ToolContext
from hermes_lite.tools.research import _clean_md, _project_pin, _sections

DOC = "# Lib\nintro\n\n## Streaming\nUse StreamingResponse.\n\n## Colors\nunrelated\n"


def test_sections_keep_only_matching_parts():
    out = _sections(DOC, "streaming response", 2000)
    assert "StreamingResponse" in out and "unrelated" not in out


def test_clean_md_drops_badges_and_html():
    md = '<p align="center"><img src="x.png"></p>\n[![ci](b.svg)](u)\n# Title\nText'
    assert _clean_md(md) == "# Title\nText"


def test_project_pin_reads_package_json_and_requirements(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"dependencies": {"express": "^4.18.2"}}')
    (tmp_path / "requirements.txt").write_text("httpx==0.27.0\n")
    ctx = ToolContext(cfg=Config(), home=tmp_path, cwd=str(tmp_path), session_id="t")
    assert _project_pin(ctx, "express").startswith("^4.18.2")
    assert _project_pin(ctx, "httpx").startswith("httpx==0.27.0")
    assert _project_pin(ctx, "rich") is None
