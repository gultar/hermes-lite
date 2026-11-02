"""Prompts as .md files: defaults, your copies, and every way back."""
import pytest

from hermes_lite import promptfiles as pf


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path))
    monkeypatch.setattr(pf, "ENABLED", True)
    pf.reload()
    yield tmp_path
    pf.reload()


@pytest.mark.parametrize("style", pf.STYLES)
def test_every_default_loads_and_fills_placeholders(home, style):
    pf.set_style(style)
    names = [p.stem for p in pf.PKG_DIR.glob("*.md")]
    assert len(names) >= 40
    left_out = {"parallel_tool_calls", "tool_use"} if style == "compact" else set()  # merged into task_completion
    for n in names:
        text = pf.get(n, n=1, excerpt="x", skills="x", plan="x", edited="", budget=1, today="d", steer_open="[",
                      steer_close="]", request="r", notes="N", path="P", current="C", facts="F", limit=9, todos="T")
        assert bool(text.strip()) == (n not in left_out), n
        assert "{{" not in text or n.startswith("tool_"), n
    pf.set_style("compact")
    assert "{{" not in pf.get("build", plan="THE PLAN", edited="") and "THE PLAN" in pf.get("build", plan="THE PLAN", edited="")


def test_your_copy_wins_and_bad_copies_fall_back(home):
    d = home / "prompts"
    d.mkdir()
    (d / "identity.md").write_text("<!-- my notes -->\nYou are my assistant. Code goes {like: this}.\n")
    assert pf.get("identity") == "You are my assistant. Code goes {like: this}."
    (d / "plan_mode.md").write_text("Plan it, but no headings.\n")          # lacks ## Goal / ## Steps
    (d / "build.md").write_text("   \n")                                    # empty
    pf.reload()
    assert "## Steps" in pf.get("plan_mode") and "{{plan}}" not in pf.get("build", plan="P", edited="")
    assert any("plan_mode.md lacks" in w for w in pf.WARNINGS) and any("build.md is empty" in w for w in pf.WARNINGS)
    (d / "loop_retry.md").write_text("Stuck on {{excerpt}} and {{oops}}.\n")
    pf.reload()
    assert pf.get("loop_retry", excerpt="X") == "Stuck on X and {{oops}}." and any("{{oops}}" in w for w in pf.WARNINGS)


def test_tool_descriptions_come_from_files(home):
    from hermes_lite.tools import REGISTRY
    d = home / "prompts"
    d.mkdir()
    (d / "tool_web_search.md").write_text("Search the web (my wording).\n")
    pf.reload()
    assert REGISTRY["web_search"].openai_schema()["function"]["description"] == "Search the web (my wording)."
    assert REGISTRY["web_search"].openai_schema()["function"]["parameters"] == REGISTRY["web_search"].schema["parameters"]


def test_switch_off_reset_and_undo(home):
    d = home / "prompts"
    path = pf.edit("identity")                       # starts as a copy of the default
    assert path == d / "identity.md" and pf.get("identity") == pf.body((pf.PKG_DIR / "identity.md").read_text())
    path.write_text("Version one.\n"); pf.snapshot_overrides()
    path.write_text("Version two.\n"); pf.snapshot_overrides(); pf.reload()
    assert pf.get("identity") == "Version two."
    pf.ENABLED = False; pf.reload()                  # prompts.overrides: false / --default-prompts
    assert pf.get("identity") != "Version two."
    pf.ENABLED = True; pf.reload()
    assert pf.undo("identity") and pf.get("identity") == "Version one."
    assert pf.reset("identity") and not path.exists() and (d / "identity.md.off").exists()
    assert pf.get("identity") == pf.body((pf.PKG_DIR / "identity.md").read_text())



def test_compact_files_shadow_existing_defaults_and_keep_required_text(home):
    for f in (pf.PKG_DIR / "compact").glob("*.md"):
        assert (pf.PKG_DIR / f.name).is_file(), f.name
        need = pf._spec(f.stem)[2]
        assert all(x in f.read_text() for x in need), f.name


def test_hermes_style_is_the_original_wording(home):
    pf.set_style("hermes")
    try:
        assert "# Finishing the job" in pf.get("task_completion")
        assert pf.get("parallel_tool_calls").startswith("# Parallel tool calls")
        assert "<prerequisite_checks>" in pf.get("execution")
        assert pf.param_descriptions("search_files") == {}
    finally:
        pf.set_style("compact")
    assert "<prerequisite_checks>" not in pf.get("execution") and "<mandatory_tool_use>" in pf.get("execution")
    assert pf.param_descriptions("search_files")["limit"] == "Maximum results"
