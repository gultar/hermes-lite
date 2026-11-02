<!--
Hermes Lite prompt: tool_web_extract
Description of the web_extract tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_web_extract.md and edit the copy.
-->
Extract content from web page URLs. Returns clean page content in markdown/text (fast, no summarization). Also works with PDF URLs. Pages within the character budget return whole; a longer page returns its beginning plus the path of the full text and the read_file offset to continue from. Each page also gives 'next'/'previous' (the following/preceding page of a tutorial or docs) and its navigation links. links_only=true returns just the page's links, in order: use it on a tutorial's contents page, put the chapters in todo_list, then read and carry out one chapter at a time. Pages built by JavaScript are rendered in a headless browser automatically.
