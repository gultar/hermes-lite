<!--
Hermes Lite prompt: skills_block
System prompt: the list of saved skills (only when there are some).
Placeholders: {{skills}}
Keep in your version: {{skills}} (Hermes Lite relies on them).
To change it: copy this file to ~/.hermes-lite/prompts/skills_block.md and edit the copy.
-->
## Skills
Before replying, scan the skills below. If a skill matches or is even partially relevant to your task, you MUST load it with skill_view(name) and follow its instructions. Err on the side of loading — it is always better to have context you don't need than to miss critical steps, pitfalls, or established workflows. Skills contain specialized knowledge — API endpoints, tool-specific commands, and proven workflows that outperform general-purpose approaches. Load the skill even if you think you could handle the task with basic tools like web_search or terminal. Skills also encode the user's preferred approach, conventions, and quality standards for tasks like code review, planning, and testing — load them even for tasks you already know how to do, because the skill defines how it should be done here.
If a skill has issues, fix it with skill_manage(action='patch').
After difficult/iterative tasks, offer to save as a skill. If a skill you loaded was missing steps, had wrong commands, or needed pitfalls you discovered, update it before finishing.

<available_skills>
{{skills}}
</available_skills>

Only proceed without loading a skill if genuinely none are relevant to the task.
