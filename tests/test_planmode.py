"""/plan and /build."""
from hermes_lite import planmode
from hermes_lite.config import load_config

PLAN = """## Goal
Fix login.
## Findings
- public/api.js:12 builds opts wrong
## Steps
1. **Fix** `opts` in `public/api.js`
   - keep headers
2) Toggle screens in `public/game.js`
## Risks and open questions
none
## Verification
- npm test
"""


def test_read_only_commands():
    for ok in ("ls -la", "git status && git diff", "grep -rn x src | head", "Get-ChildItem | Select-String x",
               "cat log 2>/dev/null", "python3 --version"):
        assert planmode.read_only_command(ok), ok
    for bad in ("rm -rf x", "npm install", "cat a > b", "sed -i s/a/b/ f", "find . -delete", "echo $(id)",
                "git push", "Remove-Item x"):
        assert not planmode.read_only_command(bad), bad


def test_plan_saved_parsed_and_handed_to_build(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    from hermes_lite.agent import Agent

    class UI:
        def event(self, *a, **k): pass
    assert planmode.looks_like_plan(PLAN) and not planmode.looks_like_plan("Sure, here is an answer.")
    assert planmode.steps(PLAN) == ["Fix `opts` in `public/api.js`", "Toggle screens in `public/game.js`"]
    a = Agent(load_config(), UI(), cwd=str(tmp_path))
    planmode.save_plan(str(tmp_path), "fix login", PLAN)
    a.plan_written = planmode.load_plan(str(tmp_path))
    a.start_plan()
    msg = a.build_message()
    assert a.mode == "normal" and "<plan>" in msg and "public/api.js:12" in msg and "edited" not in msg
    assert [t["status"] for t in a.ctx.state["todos"]] == ["in_progress", "pending"]
    (tmp_path / ".hermes" / "plan.md").write_text(PLAN.replace("npm test", "npm run e2e"))
    assert "user edited it" in a.build_message()


def test_yolo_also_skips_plan_mode_questions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    from hermes_lite.agent import Agent
    from hermes_lite.llm import ToolCall

    class UI:
        def event(self, *a, **k): pass
    cfg = load_config()
    a = Agent(cfg, UI(), cwd=str(tmp_path))
    asked = []
    a.ctx.approve = lambda command, reason: asked.append(reason) or "deny"
    a.start_plan()
    call = lambda c: ToolCall(id="t1", name="terminal", arguments="{}", parsed={"command": c})
    assert "did not allow" in a._run_tool_sync(call("touch x")) and asked      # asks while planning
    asked.clear()
    cfg["agent"]["approvals"] = "off"                                           # /yolo or --yolo
    a._run_tool_sync(call("touch x"))
    assert not asked and (tmp_path / "x").exists()
