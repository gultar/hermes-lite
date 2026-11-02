# ☤ Hermes Lite

A lightweight Hermes Agent for small local models. Same loop and prompting as Hermes Agent, only the
tools you use day to day, a Textual terminal UI, and full access to your own machine. Built around
Qwen3.6-35B-A3B on llama.cpp.

## Install

```bash
./install.sh            # venv in ~/.local/share/hermes-lite, links ~/.local/bin/hermes-lite
$EDITOR ~/.hermes-lite/config.yaml   # set server.model_path (and server.binary if not on PATH)
hermes-lite             # from the project folder you want to work in
```

Starting `hermes-lite` opens llama-server in its own terminal window (kitty, alacritty, foot, wezterm,
ghostty or konsole — `$TERMINAL` wins), waits for the model to load, and stops the server when you quit.
It uses its own port, 8091, so it never touches a llama-server another tool runs on the usual 8080.
If a server already answers on 8091 with your configured model, it is reused; if it runs a different
model, it is stopped and yours is started. `/server restart` stops whatever llama-server holds the port.

The server command is the known-good, non-MTP configuration:

```
llama-server -m ~/models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf --host 127.0.0.1 --port 8091 \
  -ngl 99 --n-cpu-moe 38 -c 64000 -fa on -ctk q8_0 -ctv q8_0 -t 8 --jinja
```

`--jinja` is the one addition: it turns on native tool calling through Qwen's chat template. No
`--spec-type` / `--spec-draft-n-max` flags; Hermes Lite refuses to start if they appear in
`server.extra_args`. Check the exact command with `hermes-lite --print-server-cmd`.

### Windows and macOS

Hermes Lite adapts to the system it runs on (`hermes_lite/oscompat.py`):

| | Linux | macOS | Windows |
|---|---|---|---|
| `terminal` tool | bash | bash | PowerShell (pwsh 7 if installed, else 5.1) |
| stopping commands | process groups | process groups | `taskkill /T` (whole tree) |
| sudo password box | yes | yes | no (no sudo) |
| memory guard, RAM budget | /proc | psutil | psutil |
| llama-server | own terminal window or headless | headless, log in `~/.hermes-lite/logs` | headless, or `server.terminal: window` |
| clipboard | wl-clipboard / xclip | pbcopy | PowerShell |
| screenshots | grim + hyprctl / X11 tools | `screencapture` (screen) | .NET (screen) |
| install | `./install.sh` | `./install.sh` | `powershell -ExecutionPolicy Bypass -File .\install.ps1` |

On Windows the model is told it is in PowerShell (cmdlets, `$env:NAME = …`), and PowerShell's
destructive commands (`Remove-Item -Recurse`, `rd /s`, `Format-Volume`, `iwr … | iex`, `-Verb RunAs`,
registry deletes…) need your approval like `rm -rf` does on Linux. Set `server.binary` to
`llama-server.exe` (or its full path). `agent.shell: powershell` also works on Linux/macOS with pwsh.

### Current library knowledge

The model's training data is older than many libraries' current versions, so it can look them up,
with no API key: `package_info` (latest version, release date, links, deprecations, and the
version this project already pins; PyPI, npm, crates.io), `github` (repository search, README,
files, releases; code search when `gh auth login` is done or `GITHUB_TOKEN` is set) and
`read_docs` (a library's docs, from its `llms.txt` when it has one, cut to the sections that match
the question). Results are short by default and cached for a week in `~/.hermes-lite/cache`.

### File outlines

When the model reads a file longer than 150 lines, `read_file` puts an outline first: every
class, function and method with its parameters, types, return type and line range (Python via
its parser; JS/TS, Vue/Svelte/HTML scripts, Go, Rust, C-family, shell, Ruby, Lua by pattern;
Markdown headings; JSON/YAML/TOML top-level keys). Files are read whole (up to 2000 lines and
`agent.read_chars`); bigger ones come back as the outline plus the first 300 lines, and the model
reads the parts it needs with `offset`/`limit`. A re-read of an unchanged file returns a short
"unchanged" note only while the earlier result is still in the conversation (not after
compaction, `/undo` or `/retry`). After 12 reads in a row without acting, the model gets a nudge
to act on what it has read.
The outline is sent once per version of a file, and `outline_only=true` gives it without the
code. `write_file` refuses to replace a file the model has only partly seen with something much
shorter. Settings: `agent.outline` (auto, always, off), `agent.outline_min_lines`,
`agent.read_full_lines`, `agent.read_head_lines`.

### Vision (screenshots)

Off until you set `server.mmproj` to the vision encoder file (or `auto` for an `mmproj*.gguf`
next to the model). The encoder runs on the GPU: it uses no RAM, so the model keeps all the RAM it
needs for fast prefill, and screenshots are read quickly. It takes some VRAM, so on a small GPU
the context may be lowered to fit; your micro-batch (prefill speed) is kept. `mmproj_offload: false`
puts it in RAM instead, but only if the RAM budget below has room.

With vision on, the model gets `screenshot` (the screen, a window by name, or a web page rendered
headless from a URL, e.g. `http://localhost:3000`) and `view_image` (an image file). Captures use
`grim` + `hyprctl` on Hyprland, `maim`/`scrot`/ImageMagick on X11, and chrome-headless-shell,
Chromium/Chrome/Brave or Firefox (throwaway profile) for URLs. Images are scaled to 1280 px.
Each image is shown to the model **once**, with a request to write down what it sees; after that
reply it is replaced by the model's own notes, so images never pile up in the context. Without
vision, these tools are not offered and the prompt tells the model it cannot see images.

### Python projects get their own virtualenv

Every command runs with the project's `.venv` (or `venv`) active when one exists in the working
folder or a parent — `python`, `pip` and installed tools point into it, no `activate` needed. The
first `pip install` in a project without one creates `.venv` in the project folder (`uv venv --seed`
if uv is installed, else `python -m venv`), so packages never land in your system Python. A leading
`cd /path/to/project &&` is followed. `--break-system-packages` is dropped inside a venv (it does
nothing there) and needs your approval anywhere else. Poetry, Pipenv and Conda projects are left
to their own tool, and a virtualenv the model activates itself is respected. Off: `agent.auto_venv: false`.

### Prompts are Markdown files

Everything the model reads — the system prompt blocks, every tool description, and the notes
Hermes Lite adds mid-conversation (plan/build, loop guard, nudges, compaction) — is a `.md` file in
`hermes_lite/prompts/`. To change one, `/prompts edit <name>` copies it to `~/.hermes-lite/prompts/`;
edit the copy and `/prompts reload`. Your copy wins and survives updates. Placeholders are written
`{{name}}` (each file's top comment lists them); only that exact form is replaced, so code and JSON in
a prompt are safe. The comment at the top is documentation and is not sent to the model.

It can't break the harness: a copy that is empty, unreadable, or lacks a placeholder or heading
Hermes Lite relies on (plan mode's `## Goal` / `## Steps`, `{{plan}}` in build…) is ignored with a
warning and the default is used; an unknown placeholder is left as written, with a warning.

Ways back, from smallest to largest:
- `/prompts undo <name>`: the previous version of your copy (the last 10 are kept, from each launch)
- `/prompts reset <name>` or `/prompts reset all`: back to the default (your copy is kept as `.md.off`)
- `hermes-lite --default-prompts`: one run with every default; `prompts.overrides: false`: for good
- `hermes-lite --rollback`: the previous version of Hermes Lite itself

### Plan, then build

`/plan <task>` puts the model in plan mode: it reads files, searches, looks up docs and runs
read-only commands (`ls`, `git status`, `grep`…; anything else asks you first), but `write_file`
and `patch` are refused. The tool list itself stays the same, so llama-server's cached system
prompt stays valid. It ends with a plan in fixed sections (Goal, Findings with file paths and line
numbers, numbered Steps, Risks and open questions, Verification), saved to `.hermes/plan.md` in the
project. Keep talking to adjust it, or edit the file yourself. `PLAN` shows in the header.

`/build` carries it out in the same conversation (what was read while planning is still there);
`/build fresh` starts a clean session with only the plan. Plan mode survives compaction (its
instructions are re-attached to the summary); after 10 and 15 investigation steps the model is
reminded to wrap up, and at `agent.plan_max_steps` (20) tools are switched off for one reply so it
writes the plan. `/build` with no plan yet asks the model to write it first, then builds. Either way the plan reaches the build
four ways, because the model's thinking is never carried over: the plan is a visible reply, it is
a file, `/build` sends the whole file (noting if you edited it) and turns its steps into the todo
list, and compaction re-attaches it to the summary during the build. `/plan off` leaves plan mode.

### Thinking loops

Qwen3.6 sometimes thinks in circles for thousands of tokens. The loop guard (`loop_guard:` in
config.yaml) stacks several defences:

- **Thinking budget:** at most 8192 thinking tokens per reply, enforced by llama-server
  (`reasoning_budget_tokens`); when it runs out, a short "time to act" line closes the thinking.
- **Loop detection:** Hermes Lite watches the *thinking* only — never the answer or a tool call — and
  only its prose: code, file contents and tool-call markup are set aside, since code repeats itself
  legitimately. When the prose keeps repeating (a stretch coming back verbatim, the same sentence 5
  times, or text that compresses like a loop) on two checks in a row, it ends
  the thinking on the spot through llama-server's `/v1/chat/completions/control` (recent builds),
  so the model goes straight to its answer. On older builds it stops the reply and resends it with
  a note ("you were going in circles about …, act now") and a 2048-token budget, at most twice.
  A 🔁 line in the chat says when this happens; `stream.log` keeps the looping text.
- **Sampling:** presence penalty 0.5 and temperature 0.7 (Qwen allows a 0–2 penalty against
  endless repetition; loops grow at low temperatures). DRY is available (`dry_multiplier`), off.
- **Prompt:** one line asking the model to stop and act when it repeats itself.

**Rollback:** `loop_guard.enabled: false` (or `hermes-lite --no-loop-guard` for one run) gives
exactly the previous behaviour: your `model.temperature` and `model.presence_penalty` apply again, and no budget, detection or prompt line. Two more levers, not
applied because they change memory use: `cache_type_k/v: bf16` (Unsloth's suggestion when output
degrades; doubles the cache's VRAM) and a higher-quality quant than IQ4_XS once RAM allows.

### Prompt style (compact / hermes)

`prompts.style: compact` (the default) uses shorter wording of the same rules, about 1,800 tokens
less (≈ 8.4k instead of 10.1k, system prompt plus tools):
- the four "keep working, verify, don't invent results" blocks merged into one ("Getting it done");
  the separate batching section and `<prerequisite_checks>` folded in
- shorter memory guidance, steering note (same marker) and coding brief (no "activate a
  virtualenv": the project's `.venv` is automatic)
- shorter descriptions for memory, terminal, write_file, patch, clarify, todo_list (guardrails
  kept) and search_files' parameters; `skills_list` left out (the skills are listed in the prompt)

Kept as they were: the must-use-a-tool list, act-don't-ask, literal preservation, identity,
environment, thinking-loop line. The compact files are in `hermes_lite/prompts/compact/`.

**Rollback:** `prompts.style: hermes` in `~/.hermes-lite/config.yaml` gives the original Hermes
wording, byte for byte; `hermes-lite --hermes-prompts` for one run; `/prompts style hermes`
switches the current session (from the next message on).

### Taking turns with another program's server (one model on the GPU at a time)

`server.stop_others: [8080]` (with Hermes Lite on its own port, 8091): at startup Hermes Lite
records exactly how the server on 8080 was started (binary, flags, folder, environment) and stops
it, then starts its own. Quitting Hermes Lite starts the other one again, unchanged
(`server.restore_on_exit`). `/server handoff` does the same without quitting, and your next message
takes the GPU back. From a terminal: `hermes-lite --give-back`.

### Sharing another program's llama-server (e.g. Tiny Code Agent on 8080)

With `server.port: 8080` (`hermes-lite --set server.port=8080`), Hermes Lite uses a llama-server
that is already running there with the same model, and takes its context size from it. `/server`
shows its command line, who started it, and how its flags differ from Hermes Lite's.
`/server restart` force-stops it (after 8 s if it does not stop on its own) and starts the very same
command again (binary, flags, folder, environment) in the background: it keeps running after you
quit Hermes Lite, like the original. `server.restart_external: own` replaces it with Hermes Lite's
settings instead, which also happens automatically when its context is too small (under 16k).
`hermes-lite --set section.key=value` changes one setting without touching the rest of the file.

### Working notes and the todo list, across sessions

The `notes` tool is the model's own notebook for a project: one- or two-line notes it writes the
moment it learns something it will need later (a decision and why, where something lives, a command
that works, a pitfall, what's next), and updates or removes when they stop being true. They are kept
in `.hermes/notes.md` (outside a project: `~/.hermes-lite/notes/`), at most 60 notes of 300
characters, and shown to the model at the start of every session and after every compaction. The
todo list of a project is saved in `.hermes/todos.json`: unfinished items come back in the next
session (sidebar and system prompt). Launch lines say what came back. You can edit `notes.md`
yourself. Off: `agent.disabled_tools: [notes]`.

### Project notes (project memory)

Inside a project (a Git repository or a folder with a project file), Hermes Lite keeps
`PROJECT_NOTES.md` at the project root: what the project is, its layout, what was done (dated), where things stand,
what comes next, and conventions, commands and pitfalls. Every session starts with it in the system
prompt, so the model picks up where it left off.

A line at launch says whether notes were read. The model writes the first version after its
first reply that used 3 tools. After that, every 8 tool calls (right after the skill review) it
writes only what changed: new "Done so far" bullets, which are appended, and the sections that
changed, which replace the old ones. That's a few hundred words instead of the whole file. Once the
list passes 30 bullets, the next update rewrites and condenses the whole file. `/notes update`
updates now, `/notes rewrite` rewrites it whole. Quitting and `/new` never wait for the notes
(`agent.project_notes_on_exit: true` brings the end-of-session update back). `/notes` shows the file. You can edit it: the next update starts from your
version. The last 10 versions are in `.hermes/notes-history/`; notes kept in `.hermes/project.md` by the
first version are moved to `PROJECT_NOTES.md` automatically. Off: `agent.project_notes: false`.

### Hermes Lite stays in your folder

Hermes Lite works in the folder you launched it from. A command that moves into a subfolder is
followed, but a project folder that gets moved, renamed or replaced is not: the path stays the one
you work in (the shell's `$PWD`, not where the old folder went), and if that path no longer exists
it falls back to the launch folder. Resuming a session keeps the folder you launched from too, unless
you launched from your home folder: then it goes back to the session's own folder, if it still
exists. `/cwd <dir>` changes it.

### The Trash counts as deleted

A folder sent to the Trash is only moved: a shell, a server or a search that was in it reports the
Trash location, and the model would carry on there. Hermes Lite refuses every tool call on a path in
the Trash (`~/.local/share/Trash`, a drive's `.Trash-1000`, `~/.Trash`), including terminal commands
that name it, and a working folder that ends up in the Trash falls back to where the session
started, with a note to the model. Off: `agent.block_trash: false`.

### Big files on GitHub

`github action=file` returns small files whole, as before. A file bigger than the budget (6000
characters) is shown the way `read_file` shows a big local file: its outline (classes, functions,
their line ranges) and the first lines. The model then reads any range with the same call plus
`offset`/`limit`, or with `read_file` on the session copy. The file is downloaded once per session
and the copy is deleted when the session ends (`/new` or quitting).

### Following a website (tutorials, docs)

`web_extract` gives each page's `next`/`previous` page (from `rel=next` links or "Next →"-style links)
and its navigation links (menus, sidebars, footers, which are kept out of the page text).
`links_only: true` returns just a page's links in order: a tutorial's table of contents. Pages built
by JavaScript are rendered automatically: in Lightpanda (a headless browser made for agents, ~30 MB and
a fraction of a second; `install.sh` puts it in the venv), else in a headless Chromium
(chrome-headless-shell, Chromium, Chrome, Brave… with a throwaway profile). `web.lightpanda: off` skips
it. Screenshots still use Chromium or Firefox: Lightpanda draws nothing. A long chapter comes back as its beginning plus the
`read_file` offset to continue from. To follow a tutorial, ask e.g. "Follow
https://os.phil-opp.com chapter by chapter in this folder": the model lists the chapters into its
todo list, then reads and carries out one chapter at a time.

### Keeping track (context)

With a 40k context, the system prompt and tool list take ~10k tokens before you type anything, so
how the rest is spent decides how long the model remembers what it is doing.

- **Thinking is not counted.** Past thinking is never sent back to the model, so it no longer
  counts towards the context. (It used to: one long reply could trigger a compaction right away.)
- **Room for the reply.** The prompt may fill `compress_at` (80 %) of the context, but never so
  much that the next reply's thinking (the loop guard's budget) and answer would not fit.
- **Trim before summarizing.** When the limit is reached, file contents and command output older
  than the recent quarter of the context are trimmed to a short note ("read_file again if you need
  it"), and so are file bodies inside old `write_file`/`patch` calls. Every message and every tool
  call (which tool, on which file) stays word for word. A ✂ line says when this happens.
- **The latest reads stay whole.** The last 3 `read_file` results (`agent.keep_recent_reads`) are
  never trimmed, however far back they are, within ~45 % of the free context (the newest one
  always); a summary keeps them in the verbatim part too. An earlier read of a file and range that
  was read again since is trimmed: the newer copy is there.
- **Summary written in place.** The summary is asked for at the end of the conversation as
  llama-server already has it cached, so it costs only the writing, not re-reading tens of
  thousands of tokens (a separate, size-capped request is only used when the context is too full
  for that). `/compress` shows its progress, the rest of the app keeps working meanwhile, and Esc
  cancels it without changing anything. `/server restart` force-stops a server that does not exit
  within 8 seconds.
- **Summary as a last resort**, with facts recorded by code attached word for word: what you asked
  (your own words), the files touched and their current length, the last commands and their exit
  codes, the model's last message, the todo list and the plan.

**Rollback:** `agent.prune_old_outputs: false` (summaries only), `agent.work_ledger: false` (no
facts list), `agent.compress_at: 0.5` and `agent.keep_recent_tokens: 12000` give the previous
settings; `hermes-lite --rollback` restores the previous code.

### Fast launches

The system prompt and tool list (~10k tokens) must be read by the model before your first message.
Hermes Lite does it at startup, then saves llama-server's cache of it to
`~/.hermes-lite/slot-cache/` (`--slot-save-path`). The next launch with the same prompt (same
project, same day, same memory) restores it in seconds instead of reading it again. llama-server
runs a single slot (`-np 1`, `server.parallel`), so the cache is always where your next message
goes. Whether your first message can reuse a restored cache depends on your llama.cpp build: recent
builds save the checkpoints Qwen3.6's hybrid attention needs.

### RAM budget

The model file has to stay in RAM for fast prefill (with experts on the CPU, every prompt batch
reads them). Hermes Lite gives the optional extras only what is left after the model and
`server.ram_desktop_gib` (5 GB for your desktop and apps): llama-server's prompt cache in RAM
(`server.cache_ram_mib`, 512 MB at most) and a RAM-side vision encoder. On a 23 GB machine with a
17.7 GB model, that is little, so they shrink or move to the GPU, and a note says so at startup.

### Rollback

Every `./install.sh` keeps a copy of the installed version (last 8), and every launch keeps
`config.yaml` when it changed (last 15).

```
hermes-lite --rollback          # previous code version
hermes-lite --rollback latest   # newest saved code version (undoes a rollback)
hermes-lite --rollback config   # previous config.yaml
hermes-lite --rollback list     # what is saved
```

### Memory safety

- Command output is never held whole in memory: Hermes Lite keeps a head and a tail, and streams
  the rest to a log file capped at 50 MB. Background logs are read from their end only.
- A memory guard checks free RAM every 2 seconds. Under 1.5 GB (or 6% of RAM), it stops the
  largest process Hermes Lite started (never llama-server), and tells you and the model.
  `agent.memory_guard`, `agent.memory_guard_min_free_gib`.
- Loading the model fully into RAM (`--load-mode none`, `--no-mmap`, `--mlock`) is refused unless
  total RAM is at least the model size plus 12 GB; memory-mapped loading is used instead.
  `server.ram_reserve_gib`, `server.allow_preload_low_ram`.
- llama-server's prompt cache in RAM is capped at 512 MB, less when RAM is short (see RAM budget).
- `~/.hermes-lite/logs/memory.log` records free RAM, Hermes Lite, llama-server and the largest
  tool process every 30 seconds.

### sudo

When a command uses `sudo`, Hermes Lite first asks you to approve the command, then shows a
masked password box. The password goes straight to sudo (via `sudo -A` and a private socket):
it never enters the conversation, the tool output, the logs or any file. Tick "remember" to skip
the box until Hermes Lite quits (kept in memory only); `/forget-password` drops it.

### Copy and paste

Select text with the mouse: it is copied as soon as you let go (also Ctrl+C). A pop-up says
whether the system clipboard really took it (Hermes Lite reads it back to check). Every copy is
also saved to `~/.hermes-lite/clipboard.txt` and sent through the terminal (OSC 52), so it is
never lost. Paste with Ctrl+V or your terminal's paste (Ctrl+Shift+V in kitty). Install
`wl-clipboard` for the system clipboard on Wayland. `/copy` copies the last answer, `/copy error`
the last error, `/export` saves the conversation as Markdown. Prefer your terminal's own
selection? Start with `hermes-lite --no-mouse` (or `ui.mouse: false`): plain drag selects, and
Ctrl+Shift+C copies, like in any terminal.

### When the GPU runs out of memory

Before you type anything, Hermes Lite sends llama-server one full-size test prompt (its system
prompt plus every tool definition). If the server crashes for lack of VRAM, at load or on that
prompt, it steps down and tries again. It keeps the micro-batch you set (prefill speed) and lowers
the context first (yours → 48k → 40k → 32k); only then does it halve the micro-batch. With
`server.ctx_size: auto` it searches from 128k down for the largest context that fits. The
setting that works is saved in `~/.hermes-lite/logs/server-tuning.json` and reused at every launch
(it is only searched again when you change a memory setting in
config.yaml, or with `/server reset`). If a saved setting runs out of memory at launch, it is
retried once before stepping down, since a browser or game may just be holding VRAM. The agent's
context window follows it. If the server still runs out of memory mid-session, Hermes Lite
restarts it one step safer and resends your message. `/server` shows the setting in use;
`/server reset` starts the search again from your config.

### Hyprland

To send the server window to a quiet workspace, set for example
`server.hyprland_rules: "workspace 9 silent"` — the window is then opened via
`hyprctl dispatch exec "[workspace 9 silent] …"`. The window class is `hermes-lite-server`, so
`windowrulev2` rules work too. A launcher keybind:

```
bind = SUPER, H, exec, kitty --class hermes-lite -e hermes-lite -C ~/projects
```

## Using it

| | |
|---|---|
| Enter / Shift+Enter (or Alt+Enter, Ctrl+J) | send / newline. Pasted multi-line text is one message |
| ✍ cards | a tool call the model is still writing (a file's content, a patch, a command), shown live as it streams; collapsed once done. For this, Hermes Lite reads the model's `<tool_call>` text itself (`model.tool_parsing: auto`) after checking with llama-server's `/apply-template` that the tools are still in the prompt; some llama-server builds otherwise send nothing until a call is finished. A call cut off by max_tokens is never run |
| Typing while it works | steers it: delivered right after the current tool results |
| Esc | interrupt (stops generation and kills the running command) |
| Ctrl+B · Ctrl+T · Ctrl+N · Ctrl+R · Ctrl+Q | sidebar · thinking blocks · new · resume · quit |
| `!command` | run a shell command yourself; not sent to the model |
| `/help` | all slash commands: `/new /resume /retry /undo /compress /usage /cwd /think /yolo /plan /build /memory /skills /learn /tools /server /stream /copy` |

Dangerous commands (recursive deletes, `sudo`, force pushes, `dd`, `mkfs`, `curl | sh`, …) open an
approval dialog: allow once, allow for the session, or deny. `--yolo` or `/yolo` turns this off.

## What's kept from Hermes Agent

- **Prompting**: Hermes' identity block, finishing-the-job, parallel tool calls, memory/skills guidance,
  mid-turn steering note, tool-use enforcement and execution discipline (both enabled for Qwen in Hermes),
  the coding-agent brief plus workspace snapshot in code folders, project context files
  (`.hermes.md`/`HERMES.md` → `AGENTS.md` chain → `CLAUDE.md` → `.cursorrules`), `SOUL.md`
  (put it in `~/.hermes-lite/` to replace the identity), skills index, memory blocks, runtime environment.
- **Loop**: native OpenAI tool calls with parallel execution of read-only tools, streamed thinking kept
  across the tool calls of the current turn, empty-reply nudges, repeated-call loop warning, iteration
  budget with a final summary, background-process notifications that wake the agent.
- **Compaction**: when the prompt would leave too little room for the next reply, old tool output is
  trimmed first (every message and tool call stays word for word); only if that is not enough,
  Hermes' structured checkpoint summary (Goal, Completed Actions, Active State, Errors & Fixes…), with
  the recent part kept verbatim, plus facts recorded by code: your requests word for word, files
  touched, last commands, the todo list and the plan. See "Keeping track" below.
- **Tools** (17, plus `screenshot` and `view_image` with vision): `terminal`, `process_manage`, `read_file`, `write_file`, `patch` (fuzzy, or by line
  numbers), `search_files` (ripgrep), `web_search`, `web_extract`, `package_info`, `github`, `read_docs`,
  `todo_list`, `memory`, `clarify`, `skills_list`, `skill_view`, `skill_manage`. Schemas and descriptions follow Hermes'. Guardrails kept: write_file refuses to
  overwrite a file you haven't read or that changed on disk, a missing `content` is sent back to the
  model, syntax checks after every write/patch, a region that fails to patch twice gets "patch it by line numbers" advice.

Switch tools off with `agent.disabled_tools: [github, read_docs, …]`: they leave the tool list, and
guidance lines and description sentences that mention them leave the prompt too, so the model
never hears of them (about 250 tokens per tool, read before every first message). `/tools` shows
what is on.

Left out: browser automation, vision, image generation, TTS, cron, kanban, subagents, messaging gateways,
MCP, plugins, cloud providers.

### Skills it writes itself

Like Hermes Agent's background review: after 10 tool-calling rounds (counted across messages),
once the model has given its answer, Hermes Lite appends a short review request to the
conversation and lets the model use only `skills_list`, `skill_view` and `skill_manage`. It
improves an existing skill or creates a class-level one, or replies "Nothing to save". The
exchange is then dropped from the history, and because the conversation is already in
llama-server's cache, only the reply costs time. Typing or Esc skips it. `/learn` runs it on
demand. Settings: `skills.auto_review`, `skills.review_after_tool_calls`,
`skills.review_thinking`. New skills join the system prompt's list from the next session on.

## Small-model safety nets

- Tool calls a model leaks into plain text (`<tool_call>{…}</tool_call>` or Qwen3-Coder's
  `<function=…><parameter=…>` form) are recovered and executed; the markup is hidden from the UI.
- Malformed JSON arguments are repaired when possible (trailing commas, Python literals,
  double-encoding), otherwise the error goes back to the model with a request to re-emit.
- Inline `<think>` text is split out if the server doesn't separate `reasoning_content`.

## Files

```
~/.hermes-lite/
  config.yaml            your settings (merged over hermes_lite/default_config.yaml)
  SOUL.md                optional identity override
  memories/MEMORY.md     agent notes   (§-separated entries, 2200-char budget)
  memories/USER.md       user profile  (1375-char budget)
  skills/<category>/<name>/SKILL.md
  sessions/*.json        resumable sessions (hermes-lite -r <id>)
  logs/llama-server.log  server output (also shown in its window)
  scratch/<session>/     TMPDIR for commands, saved full outputs; pruned after 7 days
```

To reuse your existing Hermes skills read-only: `skills.extra_dirs: ["~/.hermes/skills"]`.

Web search needs no API key: it uses DuckDuckGo's HTML endpoint, or a SearXNG instance with
`web.search_backend: searxng`.

## Tests

`python -m pytest tests` covers the tool-call parsers, think splitting, fuzzy patching and compaction
boundaries.
