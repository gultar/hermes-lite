<!--
Hermes Lite prompt: tool_process_manage
Description of the process_manage tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_process_manage.md and edit the copy.
-->
Poll, wait on, or kill background terminal processes (from terminal(background=true)). poll: status + new output. log: full output, paged. wait: block until exit or timeout (partial output on timeout). write vs submit: submit appends Enter — use it to answer prompts; write sends raw bytes, no newline. close: EOF stdin. kill: terminate.
