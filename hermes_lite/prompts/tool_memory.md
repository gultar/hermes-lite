<!--
Hermes Lite prompt: tool_memory
Description of the memory tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_memory.md and edit the copy.
-->
Save durable facts to persistent memory that survive across sessions. Memory is injected into every future session, so keep entries compact and high-signal.

HOW: make ALL your changes to one target in ONE call via an 'operations' array (each item: {action, content?, old_text?}). The batch applies atomically and the char limit is checked only on the FINAL result. One batch call finishes the update, so don't repeat it. Use the bare action/content/old_text fields only for a single lone change.

WHEN: only for facts that apply to EVERY session regardless of task: who the user is, stable environment facts, standing conventions with no task home. Procedures and pitfalls learned while doing a task belong in a skill via skill_manage.

TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your notes (environment, conventions, tool quirks, lessons).

SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, completed-work logs, temporary TODO state.
