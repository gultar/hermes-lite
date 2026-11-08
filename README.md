# Hermes Lite

Hermes Lite is a coding agent for your terminal that runs entirely on your own machine, with a
local model served by [llama.cpp](https://github.com/ggml-org/llama.cpp). You describe a task; the
model reads your files, runs commands, edits code, looks things up on the web and reports back,
while you watch every step in a terminal UI and approve anything risky.

It is a trimmed-down take on [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous
Research: the same prompting and agent loop, only the tools needed for day-to-day work, and many
safeguards for the things small local models get wrong. It is built and tuned for
**Qwen3.6-35B-A3B** on a consumer PC (a few GB of VRAM, the rest in system RAM), but works with
other models that llama-server can run with tool calling.

- Starts and manages `llama-server` for you, and finds settings that fit your GPU.
- Works in the folder you launch it from: reads, searches, writes and patches files, runs shell
  commands, manages background processes.
- Looks up current library versions and docs, reads GitHub, searches and reads the web, with no
  API keys.
- Remembers across sessions: per-project notes, the model's own working notes, open todo items,
  reusable skills, and a small memory about you.
- Plan mode: investigate first and write a plan, then build it.
- Runs on Linux, macOS and Windows.

## Requirements

- Python 3.11 or newer.
- `llama-server` from a recent llama.cpp build, and a GGUF model. The defaults assume
  Qwen3.6-35B-A3B (a 4-bit quant is about 18–21 GB).
- Enough RAM to hold the model file plus your desktop (32 GB for the model above), and a GPU with a
  few GB of VRAM. The GPU holds the attention layers and the context; the model's experts stay in
  RAM.
- Optional: `ripgrep` (faster search), `poppler` (reading PDFs), `wl-clipboard` (clipboard on
  Wayland), Chromium or Chrome (rendering pages that need JavaScript, screenshots of web pages).

## Installation

Linux and macOS:

```bash
git clone https://github.com/gultar/hermes-lite.git
cd hermes-lite
./install.sh
```

Windows (PowerShell):

```powershell
git clone https://github.com/gultar/hermes-lite.git
cd hermes-lite
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer creates a virtual environment for Hermes Lite, puts a `hermes-lite` command on your
PATH (`~/.local/bin` on Linux and macOS) and writes a starter config to
`~/.hermes-lite/config.yaml`. On Linux and macOS it also installs
[Lightpanda](https://lightpanda.io), a small headless browser used for pages that need JavaScript;
if that fails, the install continues and Chromium is used instead.

Then open `~/.hermes-lite/config.yaml` and set at least where your model is:

```yaml
server:
  model_path: ~/models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf
  binary: llama-server        # or the full path, e.g. llama-server.exe on Windows
```

Your file only needs the settings you change; everything else comes from
[`hermes_lite/default_config.yaml`](hermes_lite/default_config.yaml), which documents every option.

### Frozen offline install (Linux x86_64)

A normal install takes your system Python and the latest packages from PyPI, so a Python or package
update can break it. A bundle freezes everything instead: a standalone Python, every dependency as a
pinned wheel and, optionally, your `llama-server` with the libraries it was built against. Installing
from a bundle needs no internet; the web tools still work whenever you are online.

```bash
./make-bundle.sh                                   # needs uv and internet, once
./make-bundle.sh --llama /path/to/llama-server     # also freeze llama-server (not the NVIDIA driver)
```

This writes `vendor/` (Python, wheels, `requirements.lock`) and
`dist/hermes-lite-bundle-<build>-linux-x86_64.tar.gz`. Copy the tarball anywhere, extract it and run:

```bash
./install-offline.sh                     # or --use-bundled-llama to point server.binary at it
```

The Python is copied to `~/.local/share/hermes-lite/python-<version>` and the venv is rebuilt on it.
`hermes-lite --rollback` reinstalls from `vendor/wheels`, so it works offline too. To go back to a
regular install, run `./install.sh`. Keep `vendor/` and `dist/` out of git.

## Quick start

```bash
cd ~/projects/my-app
hermes-lite
```

Hermes Lite starts llama-server (in its own terminal window, or in the background with
`server.terminal: none`), waits for the model to load, and opens the chat. Type a task and press
Enter. Examples:

- "Add a `--verbose` flag to the CLI and a test for it."
- "Why does `pytest tests/test_api.py` fail? Fix it."
- "Follow https://os.phil-opp.com chapter by chapter in this folder."

The model works in steps: each tool call appears as a card (a file read, a command and its output,
a patch). While a file is being written you see its content stream in. When it is done, it gives a
short report of what changed and what it verified.

Useful command-line options:

| Option | Effect |
|---|---|
| `-C <dir>` | work in another folder |
| `-r <id>` | resume a saved session |
| `--no-server` | use a llama-server that is already running |
| `--yolo` | never ask before dangerous commands |
| `--print-server-cmd` | print the llama-server command it would run, and exit |
| `--set section.key=value` | change one setting in config.yaml, and exit |
| `--rollback` | go back to the previous installed version (see [Updating](#updating-and-rolling-back)) |

`hermes-lite --help` lists them all.

## Using the interface

| Key | Action |
|---|---|
| Enter | send |
| Shift+Enter, Alt+Enter, Ctrl+J | new line |
| Esc | interrupt: stops the model and any running command |
| Ctrl+T | show or hide the model's thinking |
| Ctrl+B | show or hide the sidebar (plan, todo list, background processes, session) |
| Ctrl+N / Ctrl+R / Ctrl+Q | new session / resume a session / quit |

**Steering.** You can type while the model works. Your message is delivered right after the current
tool results, and the model adjusts course.

**Approvals.** Dangerous commands (recursive deletes, `sudo`, force pushes, `dd`, `mkfs`,
`curl … | sh`, installing into the system Python, their PowerShell equivalents on Windows) open a
dialog: allow once, allow for the session, or deny. `/yolo` or `--yolo` turns this off.

**sudo.** When an approved command needs your password, a masked password box appears. The password
goes straight to sudo and never enters the conversation, the logs or any file.

**Copy and paste.** Selecting text with the mouse copies it. `/copy` copies the last answer, and
`/export` saves the whole conversation as Markdown. Start with `--no-mouse` if you prefer your
terminal's own selection.

### Slash commands

| Command | Does |
|---|---|
| `/help` | list commands and keys |
| `/new`, `/resume [id]` | start a new session, or pick a saved one |
| `/retry`, `/undo` | re-run your last message, or drop the last exchange |
| `/plan [task]`, `/plan off` | enter or leave plan mode (see below) |
| `/build [fresh]` | carry out the plan; `fresh` starts a clean session with only the plan |
| `/compress` | compact the context now |
| `/usage` | context use and speed |
| `/notes [update\|rewrite]` | show the project notes, or have the model update them now |
| `/memory`, `/skills`, `/tools` | show memory, saved skills, and which tools are on |
| `/skills browse` | a catalogue of skills (Hermes Agent's): search, read one, Enter adds it |
| `/telegram` | `on` / `off`: use this session from Telegram too; `setup` (bot token), `forget` (re-pair) |
| `/learn` | have the model review the session and save what it learned as a skill |
| `/think on\|off` | turn the model's thinking on or off |
| `/cwd <dir>` | change the working folder |
| `/server` | server status and log; `restart`, `reset`, `handoff` (see below) |
| `/vision` | vision encoder status; `ram`, `vram` or `off` changes it and restarts the server |
| `/stream` | the raw text the server has sent for the current reply |
| `/prompts` | list the prompt files; `edit`, `reset`, `undo`, `reload` |
| `/forget-password` | drop a remembered sudo password |
| `!<command>` | run a shell command yourself; it is not sent to the model |

### Plan, then build

`/plan add user accounts` puts the model in plan mode. It reads, searches and runs read-only
commands (`ls`, `git status`, `grep`…), but cannot change files; any other command asks you first.
It ends with a plan in fixed sections (Goal, Findings, Steps, Risks, Verification), saved to
`.hermes/plan.md` in your project. Discuss it, or edit the file directly. `/build` then carries it
out, with the plan's steps as the todo list.

## How it works

### The agent loop

Each message starts a loop: the model replies, and if the reply contains tool calls, Hermes Lite
runs them (read-only ones in parallel), adds the results to the conversation, and asks the model
again, until it answers without a tool call or reaches `agent.max_iterations`. The system prompt
tells the model who it is, how to finish a job (verify with real output, never invent results), how
to work in a codebase, and what machine it is running on. Project files such as `AGENTS.md`,
`CLAUDE.md`, `HERMES.md` or `.cursorrules` are added to it when present.

### Tools

| Tool | What the model can do with it |
|---|---|
| `terminal`, `process_manage` | run shell commands (bash, or PowerShell on Windows); run and watch background processes such as dev servers |
| `read_file` | read a file; long files come with an outline of their classes and functions with line ranges, and are read in parts |
| `write_file`, `patch` | write files; targeted edits by text or by line numbers; syntax checks after each change |
| `search_files` | search file contents or names (ripgrep) |
| `web_search`, `web_extract` | search the web (DuckDuckGo or your SearXNG) and read pages, including the next/previous page of a tutorial and pages that need JavaScript |
| `package_info`, `read_docs`, `github` | current versions from PyPI, npm and crates.io; library documentation; GitHub repositories, files and release notes |
| `todo_list`, `notes`, `memory` | track multi-step work; keep notes about the project and about you (see [Memory](#memory-across-sessions)) |
| `clarify` | ask you a question with choices |
| `skill_view`, `skill_manage` | use and maintain saved skills |
| `browser` | a page that stays open: click, type, choose, scroll, go back (Lightpanda, else Chromium) |
| `screenshot`, `view_image` | look at the screen, a window, a web page or an image file; only when a vision encoder is loaded |

Tools you don't need can be switched off with `agent.disabled_tools`. They are then removed from
the tool list and from the prompt, which saves context.

### The llama-server it manages

Hermes Lite runs its own llama-server on port 8091, so it never touches servers that other tools
run on the usual 8080. If a server with the right model is already running on 8091, it is reused.
The command comes from the `server` section of your config. `hermes-lite --print-server-cmd` shows
it, and `server.extra_args` adds anything else, for example a micro-batch size (`-ub 1024`).

The settings that matter most for a MoE model on a small GPU:

| Setting | Meaning |
|---|---|
| `gpu_layers` (`-ngl`) | layers on the GPU; 99 = all of them |
| `n_cpu_moe` (`--n-cpu-moe`) | layers whose experts stay in RAM; 99 = all experts in RAM, only attention on the GPU |
| `ctx_size` (`-c`) | context size in tokens (`auto` searches for the largest that fits) |
| `cache_type_k`, `cache_type_v` | precision of the context cache (`q8_0` halves its size with almost no quality loss) |
| `-ub` in `extra_args` | micro-batch: larger reads prompts faster but needs more VRAM |

**Fitting the GPU.** At startup, Hermes Lite sends the server one full-size prompt. If the server
runs out of GPU memory, while loading or on that prompt, Hermes Lite steps down to a safer setting
(a smaller context first) and tries again. The setting that works is saved and reused at every
launch; `/server reset` searches again. Hermes Lite's own context limit always follows the server's.

**Fast starts.** After reading the system prompt once, the server's cache of it is saved to disk.
Later launches restore it in seconds instead of reading about 8,000 tokens again.

**Sharing the GPU with another tool.** If another program runs its own llama-server (for example
on 8080), set `server.stop_others: [8080]`. At startup Hermes Lite records exactly how that server
was started and stops it, so only one model sits on the GPU. When you quit, or with
`/server handoff`, it starts that server again exactly as it was. `hermes-lite --give-back` does
the same from a terminal.

**Vision (screenshots).** Off by default. Point `server.mmproj` at the vision encoder made for
your model (an `mmproj-*.gguf` file), or use `auto` to take one from the model's folder.
`server.mmproj_device` decides where it runs: `vram` reads images fast but takes VRAM (about
0.9 GB at F16), so the context may be lowered to fit; `ram` uses no VRAM and leaves the context
alone, but each image takes a few seconds longer. `/vision ram|vram|off` switches it from the TUI.
Each image is shown to the model once, then replaced by the model's notes on it, so screenshots
don't pile up in the context.

### Browsing

`web_extract` reads pages and follows links. For sites that need clicking or typing (search boxes,
filters, forms, menus, pages that load more as you scroll), the `browser` tool keeps one page open
between calls. Each result is the page as text with every link, button and field numbered
(`[12] button "Search"`), and the model acts on those numbers.

It runs in Lightpanda, a headless browser made for agents (tens of MB of RAM), and switches to
Chromium when Lightpanda cannot handle a page; both with a throwaway profile, never your own
browser or logins. Hermes Lite never types passwords, and submitting a form or clicking anything
that buys, sends, deletes or signs up asks you first (unless approvals are off). An idle browser
closes after 10 minutes. It needs Playwright, which `install.sh` installs; it uses the browsers
you already have and downloads none. `web.browser: chromium` skips Lightpanda.

### Telegram

Use Hermes Lite from your phone. Hermes Lite asks Telegram for new messages itself, so nothing
on your computer has to be reachable from the internet.

1. In Telegram, create a bot with @BotFather (`/newbot`) and copy its token.
2. In Hermes Lite: `/telegram on`. It asks for the token (masked; saved in
   `~/.hermes-lite/telegram.json`, readable only by you) and shows a 6-digit code.
3. Send the code to your bot. That account is paired; nobody else is ever answered.

While it is on, your phone and the TUI share the session: what you write on either side goes
to the same conversation, the answers go to both, tool calls show as one progress message that
updates, and screenshots come as photos. Approvals come as buttons; whichever side answers
first counts. From the phone, `/stop` interrupts, `/status` shows what it is doing, `/new`
starts a new session. A sudo password is never asked for over Telegram: commands that need it
are refused when the request came from the phone.

`hermes-lite --telegram` runs with no TUI at all (it asks for the token and pairs in the
terminal the first time); add `-r <session id>` to continue a session. `telegram.auto_on: true`
turns the bot on whenever Hermes Lite starts.

### Context management

A small model has a limited context, and a coding session fills it with file contents and command
output. Hermes Lite manages it in three stages:

1. **Past thinking is never sent back** to the model, so it takes no space.
2. **Old tool output is trimmed first.** When the prompt approaches its limit (80% of the context,
   while always leaving room for the next reply), file contents and command output from earlier in
   the session are replaced by a short note ("read the file again if you need it"). Every message
   and every tool call stays word for word, and the three most recent file reads stay whole.
3. **A summary only as a last resort.** If trimming is not enough, the model writes a structured
   summary of the older part of the session. Facts recorded by Hermes Lite itself are attached to
   it word for word: your requests, the files touched, the last commands and their exit codes, the
   todo list and the plan. If the task you are working on was in the summarized part, the model is
   told it is still in progress.

`/compress` does this on demand; Esc cancels it without changing anything.

### Memory across sessions

| What | Where | Written by | Used |
|---|---|---|---|
| Project notes | `PROJECT_NOTES.md` at the project root | the model, after each stretch of work (only what changed) | read at the start of every session in that project |
| Working notes | `.hermes/notes.md` | the model, with the `notes` tool, as it learns things | shown at the start of every session and after every summary |
| Todo list | `.hermes/todos.json` | the model, with `todo_list` | unfinished items come back in the next session |
| Plan | `.hermes/plan.md` | the model, in plan mode | `/build` |
| Skills | `~/.hermes-lite/skills/` | the model, after a session with enough work, or with `/learn` | listed in every session, loaded when relevant |
| Memory about you | `~/.hermes-lite/memories/` | the model, with the `memory` tool | in every session |

**Skills catalogue.** `/skills browse` lists the skills in `~/.hermes/skills` and, if Hermes
Agent's code is in `~/.hermes/hermes-agent`, its `skills` and `optional-skills` (or the folders in
`skills.catalog`). Type to search; the right side shows the skill. ⚠ marks a skill that names
tools Hermes Lite does not have (subagents, browser control, image generation…) or another OS.
Enter copies the skill into `~/.hermes-lite/skills`; Enter again removes it. Only the skills you
add go into the model's prompt.

All of these are plain text files that you can read and edit. Project notes and working notes
exist only inside a project (a Git repository or a folder with a project file such as
`pyproject.toml` or `package.json`).

### Safeguards for small models

- **Tool calls.** Hermes Lite reads the model's tool-call text itself, so calls that end up in the
  wrong place (in the answer or in the thinking) still run, malformed arguments are repaired when
  possible, and a call cut off by the output limit is reported to the model instead of run.
- **Thinking loops.** Thinking is capped at 8,192 tokens per reply. When the thinking starts
  repeating itself, Hermes Lite ends it on the spot (or resends the request with a note), and
  sampling settings that discourage repetition are used. Settings: `loop_guard` in the config.
- **Stalling.** Empty replies, replies that only think, long streaks of reading without acting, and
  the same call repeated several times each get a short nudge.
- **Files.** `write_file` refuses to overwrite a file the model has not read, or one that changed
  on disk since. A patch that fails twice is retried by line numbers.
- **Folders.** Commands always start in the folder you launched from, even if the project folder
  is moved or replaced. Anything in the desktop's Trash is treated as deleted and refused.
- **Python.** Commands run with the project's `.venv` active; the first `pip install` creates one,
  so packages never land in the system Python.
- **Memory.** If free RAM runs low, the largest process the model started is stopped (never
  llama-server).

### Prompts

Every text the model reads (system prompt sections, tool descriptions, mid-session notes) is a
Markdown file in [`hermes_lite/prompts/`](hermes_lite/prompts). To change one, run
`/prompts edit <name>`, edit the copy it creates in `~/.hermes-lite/prompts/`, then `/prompts
reload`. Your copy takes precedence and survives updates; `/prompts reset <name>` goes back to the
default. A copy that is empty or lacks something Hermes Lite needs is ignored with a warning.

`prompts.style` chooses between `compact` (the default: shorter wording of the same rules, about
1,800 fewer tokens) and `hermes` (Hermes Agent's original wording).

## Files and folders

```
~/.hermes-lite/
  config.yaml          your settings
  prompts/             your edited prompt files
  memories/            memory about you
  skills/              saved skills
  sessions/            saved sessions (hermes-lite -r <id>)
  logs/                llama-server.log, stream.log (raw model output), memory.log
  slot-cache/          saved server caches for fast starts
  scratch/             per-session temporary files, removed after 7 days

<your project>/
  PROJECT_NOTES.md     project notes
  .hermes/             plan.md, notes.md, todos.json, notes history
```

Add `.hermes/` to your project's `.gitignore` unless you want to commit it.

## Updating and rolling back

To update, pull the new code and run `./install.sh` again. Each install keeps a copy of the
previous version, and each launch keeps a copy of `config.yaml` when it changed:

```bash
hermes-lite --rollback          # previous version
hermes-lite --rollback latest   # newest saved version (undoes a rollback)
hermes-lite --rollback config   # previous config.yaml
hermes-lite --rollback list     # what is saved
```

## Platform notes

| | Linux | macOS | Windows |
|---|---|---|---|
| Shell for the `terminal` tool | bash | bash | PowerShell |
| llama-server | own terminal window, or in the background | in the background | in the background, or its own console |
| Clipboard | wl-clipboard or xclip | pbcopy | PowerShell |
| Screenshots | grim (Wayland) or X11 tools | screencapture | .NET |
| One window | grim 1.5+ and lswt (MangoWM, niri…), or Hyprland / Sway IPC; X11: xdotool | whole screen | yes |

On Windows, set `server.binary` to `llama-server.exe` or its full path.

Window screenshots use whatever the desktop offers, found at run time (there is no desktop
setting). On Wayland compositors with the ext-foreign-toplevel-list and ext-image-copy-capture
protocols, such as MangoWM, install `lswt` (AUR) and grim 1.5 or newer. Where one window cannot be
captured, the model gets the whole screen and is told why.

## Development

```bash
python -m pytest tests
```

The code is in `hermes_lite/`:

| File | Purpose |
|---|---|
| `__main__.py` | command-line entry point |
| `agent.py` | the agent loop, context management, sessions |
| `llm.py` | streaming client for llama-server, tool-call parsing |
| `server.py` | starting and tuning llama-server |
| `prompts.py`, `promptfiles.py`, `prompts/` | system prompt assembly and prompt files |
| `compaction.py`, `worklog.py` | trimming and summaries |
| `projectnotes.py`, `planmode.py`, `loopguard.py`, `pyenv.py` | project notes, plan mode, loop guard, virtualenvs |
| `tools/` | one module per tool group |
| `tui/` | the Textual interface |
| `oscompat.py` | Linux, macOS and Windows differences |

## Acknowledgements

The prompting, agent loop and tool design follow
[Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
