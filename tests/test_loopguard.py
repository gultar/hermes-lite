"""Thinking-loop guard."""
from pathlib import Path

from hermes_lite.config import load_config
from hermes_lite.llm import LLM
from hermes_lite.loopguard import LoopDetector


def _feed(text):
    d = LoopDetector()
    for i in range(0, len(text), 37):
        if hit := d.feed(text[i:i + 37]):
            return hit
    return None


ENDPOINT = """
@router.post("/{n}", status_code=200)
async def {n}_song() -> dict:
    body, code = _handle_error(_p().{n})
    if code != 200:
        raise HTTPException(status_code=code, detail=body.get("detail"))
    return {{"status": "{n}"}}
"""
ROUTES = "".join(ENDPOINT.format(n=n) for n in ("play", "pause", "resume", "stop", "seek", "volume", "next"))


def test_code_never_counts_as_a_loop():
    # the case that stopped a real write_file: the same raise line in every endpoint
    assert _feed("Draft:\n```python\n" + ROUTES + "```\nFinal:\n```python\n" + ROUTES + "```\nWrite it.") is None
    assert _feed("Draft:\n" + ROUTES * 3) is None
    assert _feed("<tool_call>\n<function=write_file>\n<parameter=content>\n" + ROUTES * 3) is None


def test_loops_are_caught_and_normal_text_is_not():
    loop = "Hmm. " + "Should I use option A or option B? Option A is simpler but B is more flexible. " * 60
    assert _feed(loop)
    root = Path(__file__).resolve().parent.parent
    for f in ("README.md", "hermes_lite/prompts.py", "hermes_lite/tools/files.py"):
        assert _feed((root / f).read_text()[:30000]) is None, f


def test_guard_sets_sampling_and_budget_and_switches_off_cleanly():
    cfg = load_config()
    llm = LLM(cfg)
    body = llm._body([{"role": "user", "content": "hi"}], [], None)
    assert body["reasoning_budget_tokens"] == 8192 and body["presence_penalty"] == 0.5 and body["temperature"] == 0.7
    assert llm._body([{"role": "user", "content": "hi"}], [], None, think_budget=2048)["reasoning_budget_tokens"] == 2048
    cfg["loop_guard"]["enabled"] = False                  # the rollback switch: exactly the old request
    body = llm._body([{"role": "user", "content": "hi"}], [], None)
    assert "reasoning_budget_tokens" not in body and body["presence_penalty"] == 0.0 and body["temperature"] == 0.6
