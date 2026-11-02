<!--
Hermes Lite prompt: tool_terminal (compact style)
Shortened; no 'activate a virtualenv' (automatic).
prompts.style: hermes uses the original wording (../tool_terminal.md).
-->
Execute shell commands on the OS and shell named in your environment section. The working directory, files and exported variables persist between calls.
Use read_file, search_files, patch and write_file instead of cat/head/tail, grep/find/ls, sed/awk and echo/heredoc. Long output is truncated with the full text saved to a file; don't pipe through head/tail to shorten it.
Foreground (default) returns when the command finishes; to wait, run the wait in the foreground — never background a sleep or polling loop. Background is only for commands that must keep running (servers, watchers): health-check them in a separate call and manage them with process_manage. After a command changes directory, trust the result's "cwd" instead of prefixing later commands with cd.
