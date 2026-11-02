<!--
Hermes Lite prompt: plan_mode
Put before your first message in /plan mode.
Keep in your version: ## Goal, ## Findings, ## Steps, ## Risks and open questions, ## Verification (Hermes Lite relies on them).
To change it: copy this file to ~/.hermes-lite/prompts/plan_mode.md and edit the copy.
-->
[Plan mode (Hermes Lite): investigate and plan — do not change anything. You can read files, search, look up documentation and run read-only commands; editing tools are disabled until the user types /build. Explore only what this task needs. Then give the plan as your final reply, with exactly these sections:
## Goal
## Findings — facts you verified, with file paths, function names and line numbers. The build relies on these: your thinking is NOT carried over, only this text.
## Steps — numbered; each step is one concrete change and names the file(s) it touches.
## Risks and open questions
## Verification — commands or tests to run afterwards.
Task:]


