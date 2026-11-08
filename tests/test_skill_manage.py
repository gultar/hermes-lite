"""skill_manage create: the name may come only from the SKILL.md frontmatter, or need tidying."""
import json

from hermes_lite.config import load_config
from hermes_lite.tools import skills as sk
from hermes_lite.tools.base import ToolContext

BODY = "---\nname: static-webapp-htmx\ndescription: >-\n  WHEN to use: htmx apps\n---\n\n# Static Web App\nSteps.\n"


def _ctx(tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    return ToolContext(cfg=load_config(str(tmp_path / "none.yaml")), home=home, cwd=str(tmp_path), session_id="s")


def test_name_from_frontmatter(tmp_path):
    ctx = _ctx(tmp_path)
    res = json.loads(sk.skill_manage({"action": "create", "content": BODY}, ctx))     # no name argument
    assert res["success"] and (ctx.home / "skills" / "static-webapp-htmx" / "SKILL.md").read_text() == BODY
    assert "static-webapp-htmx" in sk.catalog(ctx)


def test_name_is_tidied_and_frontmatter_follows(tmp_path):
    ctx = _ctx(tmp_path)
    res = json.loads(sk.skill_manage({"action": "create", "name": "Static WebApp (HTMX)", "content": BODY}, ctx))
    assert res["results"] == ["created static-webapp-htmx"]
    res = json.loads(sk.skill_manage({"operations": [{"action": "create", "name": "Course Viewer!",
                                                       "content": BODY}]}, ctx))
    text = (ctx.home / "skills" / "course-viewer" / "SKILL.md").read_text()
    assert res["success"] and text.startswith("---\nname: course-viewer\n") and "static-webapp-htmx" not in text


def test_unusable_name_says_how_to_fix(tmp_path):
    ctx = _ctx(tmp_path)
    err = json.loads(sk.skill_manage({"action": "create", "content": "no frontmatter here"}, ctx))["error"]
    assert "name is missing" in err and "name='lowercase-with-hyphens'" in err
    err = json.loads(sk.skill_manage({"action": "patch", "name": "!!!", "old_string": "a", "new_string": "b"}, ctx))["error"]
    assert "'!!!' is not a usable skill name" in err
