<!--
Hermes Lite prompt: tool_todo_list (compact style)
Shortened.
prompts.style: hermes uses the original wording (../tool_todo_list.md).
-->
Track a task list for work with 3+ steps or several tasks from the user. For 'all N items' tasks, list every item so none is dropped. Call with no parameters to read the list; it always returns the full list.
List order is priority; only ONE item in_progress at a time; break large phases into subtasks via parent. Mark an item completed only once the work is verified, never on intent. If something fails, cancel it and add a revised item.
