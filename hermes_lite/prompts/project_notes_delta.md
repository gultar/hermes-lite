<!--
Hermes Lite prompt: project_notes_delta
The short "what changed" request for the project notes (the full rewrite is project_notes_update).
Placeholders: {{current}}, {{today}}
Keep in your version: {{current}} (Hermes Lite relies on them).
To change it: copy this file to ~/.hermes-lite/prompts/project_notes_delta.md and edit the copy.
-->
[Hermes Lite: update the project notes — changes only]
The project notes below are your memory of this project for future sessions. From the conversation above, write ONLY what changed since they were last updated. Do NOT call any tool, do NOT continue the task, do NOT repeat unchanged sections.

- `## Done so far`: only the NEW bullets (today is {{today}}), e.g. `- {{today}}: added the volume slider (static/index.html, player.js)`. They are added to the end of the list.
- `## Current state`, `## Next`, `## Layout`, `## Notes`, `## What it is`: include a section only if it changed, and then write the whole new section; it replaces the old one.
- Nothing worth noting: reply exactly `NO CHANGES`.

Be brief: facts you saw in this conversation only, no guesses.

Current notes:
{{current}}
