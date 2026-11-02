<!--
Hermes Lite prompt: tool_memory (compact style)
Shortened; how to batch changes is unchanged.
prompts.style: hermes uses the original wording (../tool_memory.md).
-->
Save durable facts to persistent memory, injected into every future session — keep entries compact and high-signal.
HOW: make ALL changes to one target in ONE call via an 'operations' array (each item: {action, content?, old_text?}); it applies atomically and the char limit is checked on the final result, so don't repeat it. Use the bare action/content/old_text fields only for a single change.
WHEN: only facts true in EVERY session (who the user is, stable environment facts, standing conventions). Procedures and pitfalls from a task belong in a skill (skill_manage).
TARGETS: 'user' = who the user is (name, role, preferences, style); 'memory' = your notes (environment, conventions, tool quirks, lessons).
SKIP: obvious or easily re-discovered facts, raw data, task progress, completed-work logs, temporary TODOs.
