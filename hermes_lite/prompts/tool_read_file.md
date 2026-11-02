<!--
Hermes Lite prompt: tool_read_file
Description of the read_file tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_read_file.md and edit the copy.
-->
Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Longer files start with an outline: every class, function and method with its parameters, types and line range (Lstart-end). Long files (over 400 lines) read without offset/limit return the outline plus the first lines: then read the part you need with offset and limit. outline_only=true returns just the outline (cheap; it does not count as reading the file). Suggests similar filenames if not found. PDFs are converted to text when pdftotext is installed. Cannot read images/binary.
