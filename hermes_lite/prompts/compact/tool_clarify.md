<!--
Hermes Lite prompt: tool_clarify (compact style)
Shortened.
prompts.style: hermes uses the original wording (../tool_clarify.md).
-->
Ask the user questions when you need a decision or clarification before going on. Put every question in `questions` (1-4) — independent questions go in ONE call. Per question: single-select (up to 6 choices, recommended one FIRST; an 'Other' row is added automatically), multi-select (multi_select=true), or open-ended (no choices). Options go only in `choices`, never in the question text. Decide low-stakes questions yourself; don't use this to confirm dangerous commands (the terminal handles that).
