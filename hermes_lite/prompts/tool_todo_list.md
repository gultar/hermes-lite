<!--
Hermes Lite prompt: tool_todo_list
Description of the todo_list tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_todo_list.md and edit the copy.
-->
Track a task list for multi-step work (3+ steps). Use for complex tasks with 3+ steps or when the user provides multiple tasks. For 'all N items' tasks, enumerate every instance as its own checklist item so none are silently dropped. Call with no parameters to read the current list.
List order is priority. Only ONE item in_progress at a time. Break large phases into subtasks via parent. Mark an item completed only after the work is verified done, never based on intent. If something fails, cancel it and add a revised item. Always returns the full current list.
