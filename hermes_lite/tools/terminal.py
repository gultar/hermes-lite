"""terminal + process_manage: shell commands on the local machine with persistent cwd/exports."""

from __future__ import annotations

import codecs
import collections
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .. import oscompat, pyenv
from .base import Tool, ToolContext, error, ok, register, short, truncate_middle

FOREGROUND_MAX_TIMEOUT = 600
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r(?!\n)")

TERMINAL_TOOL_DESCRIPTION = """Execute shell commands on the OS, shell and backend named in your environment section. The working directory, files and exported variables persist between calls: activate a virtualenv or export once, not before every command.

Use read_file, search_files, patch and write_file instead of cat/head/tail, grep/find/ls, sed/awk and echo/heredoc. Output is auto-truncated with the full text saved to a file; never pipe through head/tail to shorten it.

Foreground (default) returns as soon as the command finishes. To wait a fixed time, run the wait in the foreground; never background a sleep or polling loop. Background is only for commands that must keep running after this call returns; health-check a server in a separate call and manage jobs with process_manage. When a command changes directory, trust the result's "cwd" instead of prefixing later commands with cd."""

# Linux-relevant subset of Hermes' approval patterns.
DANGEROUS_PATTERNS = [
    (r"\brm\s+(-[^\s]*\s+)*/(\s|$|\*)", "delete in root path"),
    (r"\brm\s+-[^\s]*r", "recursive delete"),
    (r"\brm\s+--recursive\b", "recursive delete"),
    (r"\brm\s+[^\n;|&]*\s-[a-z]*r[a-z]*\b", "recursive delete (flags after operands)"),
    (r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b", "world-writable permissions"),
    (r"\bchown\s+(-[^\s]*\s+)*-R\b.*\s/", "recursive chown on system path"),
    (r"\bmkfs(\.\w+)?\b", "format filesystem"),
    (r"\bdd\s+[^\n]*\bof=/dev/", "raw write to a device"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk)", "overwrite a block device"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "power off / reboot"),
    (r"\bsystemctl\s+(stop|disable|mask|isolate)\b", "stop or disable a system service"),
    (r"\bgit\s+push\b[^\n]*(--force|-f\b|--force-with-lease)", "force push"),
    (r"\bgit\s+reset\s+--hard\b", "hard reset (discards changes)"),
    (r"\bgit\s+clean\s+-[a-z]*f", "git clean (deletes untracked files)"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "pipe remote script to shell"),
    (r"\bsudo\b", "runs as root (sudo)"),
    (r"\bpacman\s+-R", "remove system packages"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bkill(all)?\s+-9\s+(-1|1)\b", "kill all processes"),
    (r"\b(cat|less|head|tail|cp|scp|base64)\b[^\n]*~?/\.ssh/id_", "reads SSH private keys"),
    (r"--break-system-packages\b", "installs into the system Python (use the project's .venv)"),
    # Windows (PowerShell / cmd)
    (r"\bremove-item\b[^\n]*-r(ecurse)?\b", "recursive delete"),
    (r"\b(rd|rmdir)\s+[^\n]*/s\b", "recursive delete"),
    (r"\bdel\s+[^\n]*/s\b", "recursive delete"),
    (r"\bformat(-volume)?\s+[a-z]:", "format a drive"),
    (r"\b(clear-disk|initialize-disk|format-volume)\b", "erase a disk"),
    (r"\b(stop|restart)-computer\b", "power off / reboot"),
    (r"\bshutdown(\.exe)?\s+/[srp]\b", "power off / reboot"),
    (r"\b(remove-itemproperty\b|reg(\.exe)?\s+delete\b)", "edit the registry"),
    (r"\bset-executionpolicy\b", "change the script execution policy"),
    (r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b[^\n]*\|\s*(iex|invoke-expression)\b",
     "pipe remote script to the shell"),
    (r"\b(iex|invoke-expression)\b[^\n]*\b(iwr|irm|invoke-webrequest|invoke-restmethod|downloadstring)\b",
     "run a remote script"),
    (r"-verb\s+runas\b", "runs as administrator"),
    (r"\b(takeown|icacls)\b", "change file ownership or permissions"),
    (r"\bstop-service\b|\bsc(\.exe)?\s+(stop|delete)\b", "stop or remove a system service"),
]


def dangerous_reason(command: str) -> str | None:
    low = command.lower()
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, low):
            return reason
    return None


def _session_dir(ctx: ToolContext) -> Path:
    d = ctx.home / "scratch" / ctx.session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env(ctx: ToolContext) -> dict:
    env = dict(os.environ)
    env.update({"PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb", "NO_COLOR": "1",
                "GIT_TERMINAL_PROMPT": "0", "DEBIAN_FRONTEND": "noninteractive",
                "TMPDIR": str(_session_dir(ctx))})
    if oscompat.IS_WINDOWS:
        env.update({"TEMP": str(_session_dir(ctx)), "TMP": str(_session_dir(ctx)), "PYTHONIOENCODING": "utf-8"})
    return env


def _argv(ctx: ToolContext, command: str, workdir: str | None, persist: bool, venv=None) -> list[str]:
    """How to run *command*: bash on Linux/macOS, PowerShell on Windows (or when forced)."""
    if oscompat.IS_WINDOWS or ctx.cfg.get_path("agent.shell") == "powershell":
        exe = oscompat.powershell()
        if not exe:
            raise FileNotFoundError("PowerShell not found")
        script = _ps_wrapper(ctx, command, workdir, persist, venv)
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    return ["bash", "-c", _wrapper(ctx, command, workdir, persist, venv)]


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ps_wrapper(ctx: ToolContext, command: str, workdir: str | None, persist: bool, venv=None) -> Path:
    """A PowerShell script that restores the session's directory and variables, runs the command,
    keeps its exit code, and saves directory and variables for the next call."""
    d = _session_dir(ctx)
    tag = uuid.uuid4().hex[:8]
    cmdfile = d / f"cmd-{tag}.ps1"
    cmdfile.write_text(command + "\n", encoding="utf-8-sig")
    envfile, cwdfile = d / "env.json", d / "cwd"
    start = workdir or ctx.cwd
    q = _ps_quote
    lines = [
        "$ErrorActionPreference = 'Continue'",
        "$ProgressPreference = 'SilentlyContinue'",
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $OutputEncoding = [Text.Encoding]::UTF8",
        f"if (Test-Path -LiteralPath {q(str(envfile))}) {{",
        f"  try {{ $saved = Get-Content -Raw -LiteralPath {q(str(envfile))} | ConvertFrom-Json",
        "        foreach ($p in $saved.PSObject.Properties) { Set-Item -LiteralPath (\"env:\" + $p.Name) -Value $p.Value } } catch {}",
        "}",
        f"try {{ Set-Location -LiteralPath {q(start)} }} catch {{ Set-Location -LiteralPath $HOME }}",
        *pyenv.powershell_lines(venv),
        "$global:LASTEXITCODE = 0",
        "$__hl_ok = $true; $__hl_errs = $Error.Count",
        f"try {{ . {q(str(cmdfile))}; $__hl_ok = $? }} catch {{ Write-Error $_; $__hl_ok = $false }}",
        "$__hl_rc = $global:LASTEXITCODE",
        "if ((-not $__hl_ok -or $Error.Count -gt $__hl_errs) -and $__hl_rc -eq 0) { $__hl_rc = 1 }",
        f"Remove-Item -LiteralPath {q(str(cmdfile))} -ErrorAction SilentlyContinue",
    ]
    if persist:
        lines += [
            f"(Get-Location).Path | Set-Content -LiteralPath {q(str(cwdfile))} -Encoding UTF8",
            "$h = @{}; Get-ChildItem env: | ForEach-Object { $h[$_.Name] = $_.Value }",
            f"$h | ConvertTo-Json -Compress | Set-Content -LiteralPath {q(str(envfile))} -Encoding UTF8",
        ]
    lines += ["Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue", "exit $__hl_rc"]
    script = d / f"run-{tag}.ps1"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return script


def _wrapper(ctx: ToolContext, command: str, workdir: str | None, persist: bool, venv=None) -> str:
    d = _session_dir(ctx)
    cmdfile = d / f"cmd-{uuid.uuid4().hex[:8]}.sh"
    cmdfile.write_text(command + "\n", encoding="utf-8")
    q = shlex.quote
    start = workdir or ctx.cwd
    from . import askpass
    helper = askpass.helper_path(ctx)
    lines = []
    if helper:
        # Route sudo's password prompt to Hermes Lite's masked password box (never to the model).
        lines += [f"export SUDO_ASKPASS={q(helper)}",
                  'sudo() { local a=(); for x in "$@"; do [ "$x" = "-S" ] || a+=("$x"); done; command sudo -A "${a[@]}"; }',
                  "export -f sudo 2>/dev/null"]
    lines += [
        f"cd {q(start)} 2>/dev/null || cd ~",
        f"[ -f {q(str(d / 'env.sh'))} ] && source {q(str(d / 'env.sh'))} >/dev/null 2>&1",
        f"cd {q(start)} 2>/dev/null",
        *pyenv.bash_lines(venv),
        f"source {q(str(cmdfile))}",
        "__hl_rc=$?",
        f"rm -f {q(str(cmdfile))}",
    ]
    if persist:
        # $PWD, not pwd: the path as written. If the folder is moved meanwhile, pwd reports where it went
        # (even the Trash) and Hermes Lite would follow it; the path stays the user's project folder.
        lines += [f"printf '%s\\n' \"$PWD\" > {q(str(d / 'cwd'))}", f"export -p > {q(str(d / 'env.sh'))} 2>/dev/null"]
    lines.append("exit $__hl_rc")
    return "\n".join(lines)


def clean(text: str) -> str:
    return ANSI.sub("", text)


def _hints(output: str, code: int) -> list[str]:
    hints = []
    if "incorrect password" in output or "Sorry, try again" in output:
        hints.append("The sudo password was wrong. Ask the user before trying sudo again.")
    elif "a password is required" in output or "no password was provided" in output:
        hints.append("The user cancelled the sudo password prompt. Do not retry sudo; ask the user whether to "
                     "continue another way or run the command themselves.")
    elif "sudo: a terminal is required" in output:
        hints.append("sudo needs a password and there is no way to ask for it here; ask the user to run that "
                     "command themselves.")
    if code == 127 or "is not recognized as" in output:
        hints.append(f"Command not found: check the spelling or install it ({oscompat.install_hint('<tool>')}, "
                     "pip/npm for packages).")
    if oscompat.IS_WINDOWS and re.search(r"\b(grep|sed|awk|ls -la|cat <<|export |chmod)\b", output) \
            and "is not recognized" in output:
        hints.append("This is PowerShell on Windows: use PowerShell commands (Select-String, Get-ChildItem, "
                     "Get-Content, $env:NAME = 'value').")
    return hints


# Memory bounds: a command can print gigabytes (a server left in the foreground, a log tail, a
# loop). Only a head and a tail are kept in memory; the full output streams to a capped log file.
HEAD_KEEP = 64_000
TAIL_KEEP = 192_000
LOG_CAP = 50 * 1024 * 1024
PROGRESS_INTERVAL = 0.15


class _BoundedOutput:
    def __init__(self, log_path: Path):
        self.head: list[str] = []
        self.head_len = 0
        self.tail: "collections.deque[str]" = collections.deque()
        self.tail_len = 0
        self.dropped = 0
        self.total = 0
        self.log_path = log_path
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")
        self.logged = 0
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        with self.lock:
            self.total += len(text)
            if self.logged < LOG_CAP:
                self.log.write(text[: LOG_CAP - self.logged])
                self.logged += min(len(text), LOG_CAP - self.logged)
            if self.head_len < HEAD_KEEP:
                take = text[: HEAD_KEEP - self.head_len]
                self.head.append(take)
                self.head_len += len(take)
                text = text[len(take):]
                if not text:
                    return
            self.tail.append(text)
            self.tail_len += len(text)
            while self.tail_len > TAIL_KEEP and self.tail:
                old = self.tail.popleft()
                self.tail_len -= len(old)
                self.dropped += len(old)

    def close(self) -> None:
        with self.lock:
            self.log.close()

    def text(self) -> str:
        with self.lock:
            head = "".join(self.head)
            tail = "".join(self.tail)
        if self.dropped:
            return head + f"\n\n[... {self.dropped} characters omitted ...]\n\n" + tail
        return head + tail


def _python_env(ctx: ToolContext, command: str, workdir: str | None) -> tuple[str, Path | None, str]:
    """The project's virtualenv for this command (created on the first pip install)."""
    return pyenv.prepare(bool(ctx.cfg.get_path("agent.auto_venv", True)), command, Path(workdir or ctx.cwd))


def run_foreground(ctx: ToolContext, command: str, timeout: int, workdir: str | None) -> str:
    persist = not workdir
    ctx.state["current_command"] = command
    command, venv, venv_note = _python_env(ctx, command, workdir)
    if venv_note:
        ctx.progress(venv_note + "\n")
    proc = subprocess.Popen(_argv(ctx, command, workdir, persist, venv),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=_env(ctx), **oscompat.new_group_kwargs())
    ctx.state.setdefault("tool_pids", set()).add(proc.pid)
    outdir = _session_dir(ctx) / "outputs"
    outdir.mkdir(parents=True, exist_ok=True)
    buf = _BoundedOutput(outdir / f"terminal-{int(time.time() * 1000)}.log")
    cancel = ctx.state.setdefault("cancel", threading.Event())

    def reader():
        assert proc.stdout
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending, last = "", 0.0
        while True:
            raw = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not raw:
                break
            text = clean(decoder.decode(raw))
            buf.add(text)
            pending = (pending + text)[-4000:]   # the live panel only needs the latest lines
            now = time.monotonic()
            if now - last >= PROGRESS_INTERVAL:
                ctx.progress(pending)
                pending, last = "", now
        rest = clean(decoder.decode(b"", final=True))
        if rest:
            buf.add(rest)
            pending += rest
        if pending:
            ctx.progress(pending)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    started, timed_out, interrupted = time.monotonic(), False, False
    while proc.poll() is None:
        if time.monotonic() - started > timeout:
            timed_out = True
        if cancel.is_set():
            interrupted = True
        if timed_out or interrupted:
            oscompat.kill_tree(proc.pid)
            break
        time.sleep(0.05)
    proc.wait()
    t.join(timeout=2)
    buf.close()
    ctx.state.get("tool_pids", set()).discard(proc.pid)
    output = buf.text().rstrip("\n")
    trash_note = ""
    if persist:
        try:
            ctx.cwd = (_session_dir(ctx) / "cwd").read_text().strip() or ctx.cwd
        except OSError:
            pass
        from .base import in_trash
        gone = not os.path.isdir(ctx.cwd)
        if gone or (in_trash(ctx.cwd) and ctx.cfg.get_path("agent.block_trash", True)):
            start = ctx.state.get("start_cwd") or str(Path.home())
            trash_note = ((f"The working folder {ctx.cwd} no longer exists (moved or deleted). Back in {start}." if gone else
                           f"The working folder was in the Trash ({ctx.cwd}): the user deleted it. Back in {start}; "
                           "never use the Trash copy."))
            ctx.cwd = start
            try:
                (_session_dir(ctx) / "cwd").write_text(start)
            except OSError:
                pass
    limit = int(ctx.cfg.get_path("agent.tool_output_chars", 20000))
    if len(output) > limit:
        shown, _ = truncate_middle(output, limit)
        shown += (f"\n[full output ({buf.total} characters) saved to {buf.log_path}"
                  f"{', first 50 MB only' if buf.total > LOG_CAP else ''} — page through it with read_file]")
    else:
        shown = output
        try:
            buf.log_path.unlink()
        except OSError:
            pass
    result = {"output": shown, "exit_code": proc.returncode, "cwd": ctx.cwd,
              "duration_s": round(time.monotonic() - started, 2)}
    if timed_out:
        result["error"] = f"Timed out after {timeout}s and was killed. Use a larger timeout, or background=true for long-running jobs."
    if interrupted:
        result["error"] = "Interrupted by the user."
    if reason := ctx.state.get("killed_for_memory", {}).pop(proc.pid, None):
        result["error"] = reason
    if hints := _hints(output, proc.returncode):
        result["hint"] = " ".join(hints)
    if venv_note:
        result["note"] = venv_note
    if trash_note:
        result["note"] = (result.get("note", "") + " " + trash_note).strip()
    return ok(**result)


# ── background processes ─────────────────────────────────────────────────
class BgProcess:
    def __init__(self, ctx: ToolContext, command: str, workdir: str | None, notify, ):
        self.id = "proc_" + uuid.uuid4().hex[:12]
        self.command = command
        self.started = time.time()
        self.log = _session_dir(ctx) / f"{self.id}.log"
        self.poll_offset = 0
        self.notified_patterns: set[str] = set()
        fh = open(self.log, "wb")
        command, venv, _ = _python_env(ctx, command, workdir)
        self.popen = subprocess.Popen(_argv(ctx, command, workdir, False, venv),
                                      stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                                      env=_env(ctx), **oscompat.new_group_kwargs())
        fh.close()
        ctx.state.setdefault("tool_pids", set()).add(self.popen.pid)
        self.notify = notify
        threading.Thread(target=self._watch, args=(ctx,), daemon=True).start()

    def text(self, tail_bytes: int = 200_000) -> str:
        """The last part of the log only: background logs can grow without limit."""
        try:
            with open(self.log, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - tail_bytes))
                data = fh.read()
            text = clean(data.decode("utf-8", errors="replace"))
            return text if size <= tail_bytes else "[... earlier output omitted ...]\n" + text.split("\n", 1)[-1]
        except OSError:
            return ""

    def read_new(self, cap: int = 64_000) -> str:
        """Output appended since the last poll (at most *cap* bytes, the newest)."""
        try:
            with open(self.log, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                start = max(self.poll_offset, size - cap)
                skipped = start - self.poll_offset
                fh.seek(start)
                data = fh.read(size - start)
            self.poll_offset = size
            text = clean(data.decode("utf-8", errors="replace"))
            return (f"[... {skipped} bytes skipped ...]\n" if skipped > 0 else "") + text
        except OSError:
            return ""

    def status(self) -> str:
        code = self.popen.poll()
        return "running" if code is None else f"exited ({code})"

    def _watch(self, ctx: ToolContext):
        patterns = self.notify if isinstance(self.notify, list) else []
        while self.popen.poll() is None:
            if patterns:
                text = self.text(64_000)
                for p in patterns:
                    if p not in self.notified_patterns and re.search(p, text):
                        self.notified_patterns.add(p)
                        ctx.notify(f"[Background process {self.id} printed a line matching {p!r}]\n"
                                   + "\n".join(text.splitlines()[-15:]))
            time.sleep(1.0)
        if self.notify is True or patterns:
            tail = "\n".join(self.text().splitlines()[-30:])
            ctx.notify(f"[Background process {self.id} exited with code {self.popen.returncode}: {short(self.command, 120)}]\n{tail}")

    def kill(self):
        oscompat.kill_tree(self.popen.pid, grace=0.3)


def procs(ctx: ToolContext) -> dict[str, BgProcess]:
    return ctx.state.setdefault("procs", {})


def kill_all(ctx: ToolContext) -> None:
    for p in procs(ctx).values():
        if p.popen.poll() is None:
            p.kill()
    from . import askpass
    askpass.close(ctx)


def terminal(args: dict, ctx: ToolContext) -> str:
    command = args.get("command")
    if not command and "code" in args:
        return error("terminal received a 'code' parameter, but it requires a shell command in 'command'.")
    if not isinstance(command, str) or not command.strip():
        return error("terminal: missing required field 'command'.")
    workdir = args.get("workdir")
    if workdir:
        workdir = str(ctx.resolve(workdir))
    if ctx.cfg.get_path("agent.approvals", "smart") != "off":
        checked = command
        if pyenv.will_have_venv(bool(ctx.cfg.get_path("agent.auto_venv", True)), command, Path(workdir or ctx.cwd)):
            checked = pyenv.BREAK.sub("", command)  # inside the project's venv the flag does nothing
        reason = dangerous_reason(checked)
        approved = ctx.state.setdefault("approved_reasons", set())
        if reason and reason not in approved:
            verdict = ctx.approve(command, reason)
            if verdict == "session":
                approved.add(reason)
            elif verdict != "once":
                return error(f"The user denied this command ({reason}). Do not retry it; "
                             "choose another approach or ask the user.", exit_code=-1)
    timeout = int(args.get("timeout") or ctx.cfg.get_path("agent.terminal_timeout", 180))
    background = bool(args.get("background", False))
    notify = args.get("notify")
    if not background and timeout > FOREGROUND_MAX_TIMEOUT:
        background, notify = True, True
    if background:
        ctx.state["current_command"] = command
        p = BgProcess(ctx, command, workdir, notify if notify is not None else False)
        procs(ctx)[p.id] = p
        time.sleep(0.5)
        early = p.text()[-2000:]
        msg = {"session_id": p.id, "status": p.status(), "output_so_far": early,
               "note": "Running in the background. Use process_manage with this session_id to poll, wait, read logs or kill it."}
        if notify is True or (timeout > FOREGROUND_MAX_TIMEOUT and not args.get("background")):
            msg["note"] += " You will be notified when it exits; do not re-run it."
        return ok(**msg)
    return run_foreground(ctx, command, timeout, workdir)


def process_manage(args: dict, ctx: ToolContext) -> str:
    action = args.get("action", "list")
    table = procs(ctx)
    if action == "list":
        return ok(processes=[{"session_id": p.id, "command": short(p.command, 100), "status": p.status(),
                              "age_s": int(time.time() - p.started)} for p in table.values()])
    sid = str(args.get("session_id") or "")
    matches = [p for k, p in table.items() if k.startswith(sid) or k.startswith("proc_" + sid)] if sid else []
    if len(matches) != 1:
        return error(f"No unique background process matches session_id {sid!r}.",
                     known=[p.id for p in table.values()])
    p = matches[0]
    if action == "poll":
        shown, _ = truncate_middle(p.read_new(), 8000)
        return ok(session_id=p.id, status=p.status(), new_output=shown)
    if action == "log":
        lines = p.text(2_000_000).splitlines()
        limit = int(args.get("limit") or 200)
        offset = args.get("offset")
        start = max(0, len(lines) - limit) if offset is None else max(0, int(offset))
        return ok(session_id=p.id, status=p.status(), total_lines=len(lines), offset=start,
                  log="\n".join(lines[start:start + limit]))
    if action == "wait":
        timeout = int(args.get("timeout") or 60)
        cancel = ctx.state.setdefault("cancel", threading.Event())
        end = time.monotonic() + timeout
        while p.popen.poll() is None and time.monotonic() < end and not cancel.is_set():
            time.sleep(0.2)
        tail, _ = truncate_middle(p.text(), 8000)
        return ok(session_id=p.id, status=p.status(), output=tail,
                  **({"note": "Still running after the wait timeout."} if p.popen.poll() is None else {}))
    if action == "kill":
        p.kill()
        return ok(session_id=p.id, status=p.status())
    if action in ("write", "submit"):
        data = str(args.get("data") or "") + ("\n" if action == "submit" else "")
        try:
            assert p.popen.stdin
            p.popen.stdin.write(data.encode())
            p.popen.stdin.flush()
        except (OSError, AssertionError) as exc:
            return error(f"Could not write to stdin: {exc}")
        return ok(session_id=p.id, status=p.status(), written=len(data))
    if action == "close":
        try:
            assert p.popen.stdin
            p.popen.stdin.close()
        except (OSError, AssertionError):
            pass
        return ok(session_id=p.id, status=p.status())
    return error(f"Unknown action {action!r}.")


register(Tool(
    name="terminal", emoji="💻",
    schema={"name": "terminal", "description": TERMINAL_TOOL_DESCRIPTION, "parameters": {
        "type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
            "background": {"type": "boolean", "default": False,
                           "description": "Keep running after this call returns; returns a session_id."},
            "timeout": {"type": "integer", "minimum": 1,
                        "description": f"Max seconds (default 180, foreground max {FOREGROUND_MAX_TIMEOUT}). Finishing early returns at once, so set it generously. Above {FOREGROUND_MAX_TIMEOUT}s the command runs as a tracked background process that notifies on exit (do not re-run it)."},
            "workdir": {"type": "string", "description": "Absolute working directory for this command only."},
            "notify": {"description": "With background=true. true: one notification on exit; use it for anything that ends (builds, tests). ['pattern', ...]: notify when a line matches, for one-shot readiness on processes that never exit. Omit for servers/daemons.",
                       "anyOf": [{"type": "boolean"}, {"type": "array", "items": {"type": "string"}}]},
        }, "required": ["command"]}},
    handler=terminal, label=lambda a: short(a.get("command", ""), 90),
))

register(Tool(
    name="process_manage", emoji="⏱",
    schema={"name": "process_manage", "description": (
        "Poll, wait on, or kill background terminal processes (from terminal(background=true)). "
        "poll: status + new output. log: full output, paged. wait: block until exit or timeout (partial output on "
        "timeout). write vs submit: submit appends Enter — use it to answer prompts; write sends raw bytes, no "
        "newline. close: EOF stdin. kill: terminate."), "parameters": {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"]},
            "session_id": {"type": "string", "description": "From terminal background output; any unique prefix works. Required except for 'list'."},
            "data": {"type": "string", "description": "Stdin text for write/submit."},
            "timeout": {"type": "integer", "minimum": 1, "description": "Max seconds for 'wait'."},
            "offset": {"type": "integer", "description": "Log line offset (default: last 200)."},
            "limit": {"type": "integer", "minimum": 1, "description": "Max log lines."},
        }, "required": ["action"]}},
    handler=process_manage, read_only=False,
    label=lambda a: f"{a.get('action', '')} {a.get('session_id', '')}".strip(),
))
