"""File outlines and how read_file uses them."""
import json
from pathlib import Path

from hermes_lite.config import Config
from hermes_lite.tools.base import ToolContext
from hermes_lite.tools.files import read_file, write_file
from hermes_lite.tools.outline import outline

JS = """const API_BASE = '/api';

function escapeHtml(str) {
  return str;
}

class Player {
  constructor(audio, {volume = 0.8} = {}) {
    this.audio = audio;
  }

  async play(track) {
    await this.audio.play();
  }

  static fromElement(el, options = {}) {
    return new Player(el, options);
  }
}

res.send = function send(body) {
  function helper(x) {
    return x;
  }
  return helper(body);
};
"""


def test_js_classes_methods_and_member_functions():
    o = outline(Path("app.js"), JS)
    assert "class Player" in o
    assert "  L8-10      constructor(audio, {volume = 0.8} = {})" in o
    assert "async play(track)" in o and "static fromElement(el, options = {})" in o
    assert "res.send = function(body)" in o
    assert "\n  L" in o.split("res.send")[1]  # nested helper is indented under res.send


def test_python_signatures_with_types():
    src = "class A:\n    def f(self, x: int, *, y: str = 'a') -> bool:\n        return True\n"
    assert "def f(self, x: int, *, y: str = 'a') -> bool" in outline(Path("a.py"), src)


def _ctx(tmp_path):
    return ToolContext(cfg=Config(), home=tmp_path, cwd=str(tmp_path), session_id="t")


def test_files_are_read_whole_with_outline_and_huge_ones_by_parts(tmp_path):
    body = "".join(f"def f{i}(a: int) -> int:\n    return a\n\n" for i in range(300))   # 900 lines
    (tmp_path / "mid.py").write_text(body)
    ctx = _ctx(tmp_path)
    r = json.loads(read_file({"path": "mid.py", "_call_id": "c1"}, ctx))
    assert "def f0(a: int) -> int" in r["outline"] and "next_offset" not in r      # whole file + outline
    huge = "".join(f"def g{i}(a: int) -> int:\n    return a\n\n" for i in range(1000))  # 3000 lines
    (tmp_path / "big.py").write_text(huge)
    r = json.loads(read_file({"path": "big.py", "_call_id": "c2"}, ctx))
    assert r["next_offset"] == 301 and "Long file" in r["note"]
    w = json.loads(write_file({"path": "big.py", "content": "def f0(a):\n    return a\n"}, ctx))
    assert "Refused" in w["error"] and (tmp_path / "big.py").read_text() == huge


def test_unchanged_only_while_the_earlier_read_is_still_in_context(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    ctx = _ctx(tmp_path)
    json.loads(read_file({"path": "a.py", "_call_id": "c1"}, ctx))
    ctx.state["live_call_ids"] = {"c1"}
    assert json.loads(read_file({"path": "a.py", "_call_id": "c2"}, ctx)).get("unchanged")
    ctx.state["live_call_ids"] = set()          # compacted / undone: the old result is gone
    assert "1|x = 1" in json.loads(read_file({"path": "a.py", "_call_id": "c3"}, ctx))["content"]


def test_outline_only_does_not_count_as_read(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    ctx = _ctx(tmp_path)
    r = json.loads(read_file({"path": "m.py", "outline_only": True}, ctx))
    assert "outline" in r
    w = json.loads(write_file({"path": "m.py", "content": "x = 2\n"}, ctx))
    assert "not read it" in w["error"]
