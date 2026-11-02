"""clarify: ask the user structured questions through the TUI."""

from __future__ import annotations

from .base import Tool, ToolContext, error, ok, register, short

MAX_QUESTIONS, MAX_CHOICES = 4, 6


def clarify(args: dict, ctx: ToolContext) -> str:
    questions = args.get("questions")
    if isinstance(args.get("question"), str):  # tolerate the single-question shape
        questions = [{"question": args["question"], "choices": args.get("choices")}]
    if not isinstance(questions, list) or not questions:
        return error("clarify needs 'questions': a list of {question, choices?, multi_select?}.")
    norm = []
    for q in questions[:MAX_QUESTIONS]:
        if isinstance(q, str):
            q = {"question": q}
        if not isinstance(q, dict) or not q.get("question"):
            return error("Each question needs a 'question' text.")
        norm.append({"question": str(q["question"]),
                     "choices": [str(c) for c in (q.get("choices") or [])][:MAX_CHOICES],
                     "multi_select": bool(q.get("multi_select"))})
    answers = ctx.clarify(norm)
    if not answers:
        return ok(outcome="cancelled", responses=[],
                  notice="The user dismissed the questions. Make a reasonable choice yourself and say which.")
    responses = [{"question": q["question"], "status": "answered" if a else "skipped", "user_response": a or None}
                 for q, a in zip(norm, answers)]
    return ok(outcome="submitted", responses=responses)


register(Tool(
    name="clarify", emoji="❓",
    schema={"name": "clarify", "description": (
        "Ask the user one or more questions when you need a decision, clarification, or feedback before "
        f"proceeding. Pass every question in `questions` (1-{MAX_QUESTIONS} entries) — several INDEPENDENT "
        "questions belong in ONE call. Per question: single-select (up to "
        f"{MAX_CHOICES} choices — put your recommended option FIRST; an 'Other' free-text row is added "
        "automatically), multi-select (multi_select=true), or open-ended (omit choices). Options go ONLY in "
        "`choices`, never inside the question text. Prefer deciding low-stakes questions yourself; don't use "
        "this for dangerous-command confirmation (the terminal tool handles that)."), "parameters": {
        "type": "object", "properties": {
            "questions": {"type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS, "items": {
                "type": "object", "properties": {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CHOICES},
                    "multi_select": {"type": "boolean"},
                }, "required": ["question"]}},
        }, "required": ["questions"]}},
    handler=clarify,
    label=lambda a: short("; ".join(q.get("question", "") if isinstance(q, dict) else str(q)
                                    for q in (a.get("questions") or [])), 90),
))
