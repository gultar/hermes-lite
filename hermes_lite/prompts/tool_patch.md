<!--
Hermes Lite prompt: tool_patch
Description of the patch tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_patch.md and edit the copy.
-->
Targeted edits in files. Use this instead of sed/awk in terminal. Two ways:
1. old_string + new_string: find a unique string and replace it. Fuzzy matching tolerates whitespace, indentation and quoting differences.
2. start_line + end_line + new_string: replace those lines (numbers exactly as read_file shows them) with new_string. Best for code with quotes or backslashes, or after old_string failed. Insert without replacing: end_line = start_line - 1. Requires a read_file of the current file first.
Returns a diff and the edited region with its new line numbers. Auto-runs syntax checks. Never rewrite a large file to make a small change.
