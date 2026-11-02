<!--
Hermes Lite prompt: tool_clarify
Description of the clarify tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_clarify.md and edit the copy.
-->
Ask the user one or more questions when you need a decision, clarification, or feedback before proceeding. Pass every question in `questions` (1-4 entries) — several INDEPENDENT questions belong in ONE call. Per question: single-select (up to 6 choices — put your recommended option FIRST; an 'Other' free-text row is added automatically), multi-select (multi_select=true), or open-ended (omit choices). Options go ONLY in `choices`, never inside the question text. Prefer deciding low-stakes questions yourself; don't use this for dangerous-command confirmation (the terminal tool handles that).
