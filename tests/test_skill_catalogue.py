"""/skills browse: Hermes Agent's skills to pick from; only the ones you add reach the prompt."""
import asyncio

from hermes_lite import skillcatalog as sc
from hermes_lite.config import load_config


def _skill(root, cat, name, front="", body="Steps."):
    d = root / cat / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} does things\n{front}---\n\n{body}\n")
    (d / "references").mkdir()
    (d / "references" / "notes.md").write_text("ref")
    return d


def _setup(tmp_path):
    src = tmp_path / "hermes-skills"
    _skill(src, "software-development", "requesting-code-review", body="Call delegate_task, then browser_exec.")
    _skill(src, "apple", "imessage", "platforms: [macos]\n")
    _skill(src, "research", "arxiv")
    (src / ".hub" / "x").mkdir(parents=True)
    (src / ".hub" / "x" / "SKILL.md").write_text("---\nname: hidden\n---\n")
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg["skills"]["catalog"] = [str(src)]
    home = tmp_path / "home"
    (home / "skills" / "mine" / "arxiv").mkdir(parents=True)        # one of your own, same name
    (home / "skills" / "mine" / "arxiv" / "SKILL.md").write_text("---\nname: arxiv\ndescription: mine\n---\n")
    return cfg, home


def test_entries_hints_add_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_PLATFORM", "linux")
    cfg, home = _setup(tmp_path)
    items = {e["name"]: e for e in sc.entries(cfg, home)}
    assert set(items) == {"requesting-code-review", "imessage", "arxiv"}
    assert items["imessage"]["hints"] == ["macos only"]
    assert items["requesting-code-review"]["hints"] == ["uses delegate_task, browser tools (not in Hermes Lite)"]
    assert items["arxiv"]["added"] and not items["arxiv"]["removable"]      # yours: never removed from here
    assert sc.matches(items["requesting-code-review"], "code review") and not sc.matches(items["arxiv"], "code")
    dest = sc.add(items["requesting-code-review"], home)
    assert (dest / "SKILL.md").is_file() and (dest / "references" / "notes.md").is_file()
    assert dest == home / "skills" / "software-development" / "requesting-code-review"
    again = {e["name"]: e for e in sc.entries(cfg, home)}["requesting-code-review"]
    assert again["added"] and again["removable"]
    sc.remove("requesting-code-review", home)
    assert not dest.exists()
    try:
        sc.remove("arxiv", home)
        raise AssertionError("removed one of your own skills")
    except PermissionError:
        pass


def test_browser_screen_adds_a_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    cfg, home = _setup(tmp_path)
    from hermes_lite.tui.app import HermesApp

    async def go():
        app = HermesApp(cfg, cwd=str(tmp_path), start_server=False)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.3)
            await app._command("/skills browse")
            await pilot.pause(0.3)
            await pilot.press(*"review")
            await pilot.press("enter")          # search → list
            await pilot.press("enter")          # add the highlighted skill
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.3)
            return "requesting-code-review" in app.agent.messages[0]["content"]
    assert asyncio.run(go()) is True
    assert (home / "skills" / "software-development" / "requesting-code-review" / sc.MARK).is_file()
