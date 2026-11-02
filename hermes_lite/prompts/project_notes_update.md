<!--
Hermes Lite prompt: project_notes_update
The request to rewrite the project notes, sent at the end of the conversation.
Placeholders: {{current}}, {{facts}}, {{today}}, {{limit}}
Keep in your version: {{current}} (Hermes Lite relies on them).
To change it: copy this file to ~/.hermes-lite/prompts/project_notes_update.md and edit the copy.
-->
[Hermes Lite: update the project notes]
Rewrite this project's notes file so that a future session — which starts with nothing but these notes — knows what the project is, what has been done and where things stand. Use the conversation above, the current notes and the facts below: keep what is still true, correct what changed, drop what no longer matters. Do NOT call any tool and do NOT continue the task: reply with the complete new file only, in Markdown, in this structure:

# <project name>
## What it is
Two to five sentences: purpose, main parts, stack, how to run it.
## Layout
The important files and folders, one line each (path — role).
## Done so far
Dated bullets, oldest first (today is {{today}}): what was built, changed or fixed, and why when it matters. Merge small steps; stay under 25 bullets by condensing the oldest ones.
## Current state
What works, what is broken or unfinished, test status.
## Next
Open tasks and the next steps the user asked for.
## Notes
Conventions, useful commands, decisions and their reasons, pitfalls, the user's preferences for this project.

Stay under {{limit}} words. Write only what you saw in this conversation or in the current notes — no guesses.

Current notes:
{{current}}

Facts recorded by Hermes Lite in this session:
{{facts}}
