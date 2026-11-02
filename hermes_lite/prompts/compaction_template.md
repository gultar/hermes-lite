<!--
Hermes Lite prompt: compaction_template
The structure the model fills in when compacting history.
Placeholders: {{budget}}, {{today}}
To change it: copy this file to ~/.hermes-lite/prompts/compaction_template.md and edit the copy.
-->
## Historical Task Snapshot
[What the user asked for in the compacted turns, in their words where it matters]

## Goal
[The overall goal being worked toward]

## Constraints & Preferences
[Requirements, conventions and preferences the user stated]

## Completed Actions
[Numbered list of concrete actions taken — include tool used, target, and outcome.
Format each as: N. ACTION target — outcome [tool: name]
Example:
1. READ config.py:45 — found `==` should be `!=` [tool: read_file]
2. PATCH config.py:45 — changed `==` to `!=` [tool: patch]
3. TEST `pytest tests/` — 3/50 failed: test_parse, test_validate, test_edge [tool: terminal]
Be specific with file paths, commands, line numbers, and results.]

## Active State
[Current working state — include:
- Working directory and branch (if applicable)
- Modified/created files with brief note on each
- Test status (X/Y passing)
- Any running processes or servers (with their session_id)
- Environment details that matter]

## Blocked
[Any blockers, errors, or issues not yet resolved. Include exact error messages.]

## Key Decisions
[Important technical decisions and WHY they were made]

## Errors & Fixes
[Errors hit during the compacted turns and how each was resolved — include the exact error text. Pay special
attention to corrections the USER gave; quote the user's correction and record what changed as a result.]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit
preservation. NEVER include API keys, tokens, passwords, or credentials — write [REDACTED] instead.]

Target ~{{budget}} tokens. Be CONCRETE — include file paths, command outputs, error messages, line numbers, and
specific values. Avoid vague descriptions like "made some changes" — say exactly what changed.

TEMPORAL ANCHORING: The current date is {{today}}. When an action has already been carried out, phrase it as a
completed, dated, past-tense fact rather than an open instruction. Never leave a finished action worded as if it
still needs doing.

Write only the summary body. Do not include any preamble or prefix.
