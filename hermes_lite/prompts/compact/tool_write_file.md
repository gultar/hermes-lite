<!--
Hermes Lite prompt: tool_write_file (compact style)
Shortened; the read-first rule and syntax checks are unchanged.
prompts.style: hermes uses the original wording (../tool_write_file.md).
-->
Write a whole file, replacing any existing content (creates parent directories). Use instead of echo/heredoc in terminal; for targeted edits use patch, never a full rewrite. For an EXISTING file, read_file it first: write_file refuses (file untouched) when this task has no current read/write of it or it changed on disk since — then read_file, merge, and retry. Syntax checks run on .py/.json/.yaml/.toml/.sh and report only NEW errors. verified:true means the write landed — don't re-read to check. Prefer several small, focused files over one huge file.
