<!--
Hermes Lite prompt: tool_write_file
Description of the write_file tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_write_file.md and edit the copy.
-->
Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. For an EXISTING file, call read_file first: write_file refuses (file untouched) when this task has no current read/write of the file or the file changed on disk since; on refusal, read_file, merge, then retry. Auto-runs syntax checks on .py/.json/.yaml/.toml/.sh; only NEW errors introduced by this write are surfaced. The result's verified:true means the write landed — do NOT re-read the file to check it. Prefer several small, focused files over one huge file. To change part of an existing file, use patch, not a full rewrite.
