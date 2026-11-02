<!--
Hermes Lite prompt: tool_search_files
Description of the search_files tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_search_files.md and edit the copy.
-->
Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents.

Content search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.

File search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls.
