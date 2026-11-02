<!--
Hermes Lite prompt: skill_review
The background skill review request.
Placeholders: {{skills}}
Keep in your version: {{skills}} (Hermes Lite relies on them).
To change it: copy this file to ~/.hermes-lite/prompts/skill_review.md and edit the copy.
-->
[Skill review: an internal step run by Hermes Lite, not a message from the user]
Look back over this session and decide whether a skill should be saved or updated, so that a
future session handles this kind of task better.

Act if any of these happened:
- The user corrected your approach, your steps, your style or your output.
- You worked out a technique, fix, workaround or command sequence that took real effort.
- A skill you used turned out wrong, incomplete or outdated.

Existing skills:
{{skills}}

Rules:
- Prefer improving an existing skill over creating a new one: skill_view it first, then
  skill_manage action=patch.
- A new skill covers a CLASS of task. Name it after the kind of work (for example webapp-verify,
  python-packaging), never after today's project, file, error or date.
- SKILL.md is YAML frontmatter (name, and a description that says WHEN to use the skill) followed
  by short numbered steps and a Pitfalls list. Keep it under 60 lines. Commands go in code blocks.
- Do not save project-specific facts, secrets, one-off results, dead ends, or things any
  competent developer already knows.
- You may only use skills_list, skill_view and skill_manage here.

If nothing is worth saving, reply exactly: Nothing to save.
Otherwise make the change, then reply with one line saying what you saved and why.
