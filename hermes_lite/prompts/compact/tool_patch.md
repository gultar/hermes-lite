<!--
Hermes Lite prompt: tool_patch (compact style)
Shortened; both modes and the line-number fallback are unchanged.
prompts.style: hermes uses the original wording (../tool_patch.md).
-->
Targeted edits in a file (instead of sed/awk). Two ways:
1. old_string + new_string: replace a unique string; fuzzy matching tolerates whitespace, indentation and quoting differences.
2. start_line + end_line + new_string: replace those lines (numbers exactly as read_file shows them). Best for code with quotes or backslashes, or after old_string failed. To insert without replacing: end_line = start_line - 1. Needs a read_file of the current file first.
Returns a diff and the edited region with its new line numbers; syntax checks run automatically.
