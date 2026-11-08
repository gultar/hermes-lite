"""llama-server lifecycle: build the command, open it in its own window, wait for health, stop."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx

from . import oscompat
from .config import Config, home


# Fallback settings tried in order when llama-server runs out of GPU memory (at load, or while
# reading a prompt). Each step frees VRAM: first halve the micro-batch you configured, then move
# every expert to the CPU, then shrink the context. Built from your config, so a larger -ub in
# extra_args gets intermediate steps (1024 -> 512 -> 256 ...) instead of jumping straight to 256.
def _user_ubatch(cfg: Config) -> int:
    extra = [str(a) for a in (cfg["server"].get("extra_args") or [])]
    for flag in ("-ub", "--ubatch-size"):
        if flag in extra and extra.index(flag) + 1 < len(extra):
            try:
                return int(extra[extra.index(flag) + 1])
            except ValueError:
                pass
    return 512


AUTO_CONTEXTS = (131072, 98304, 65536, 49152, 32768)


def _ctx_setting(cfg: Config) -> str | int:
    v = cfg["server"].get("ctx_size", 64000)
    return "auto" if str(v).strip().lower() in ("auto", "max") else int(v)


def ladder_for(cfg: Config) -> list[dict]:
    """The settings tried, in order, until llama-server loads and runs a full-size prompt.

    The micro-batch is never changed: it sets how fast prompts are read, and a smaller one makes
    every prefill slower. Only the context gives way, 1k at a time — a failed try dies within a
    second or two, before any work, and the setting that works is saved for later launches.
    ctx_size: auto starts at 128k and comes down in bigger steps first."""
    s = cfg["server"]
    ub = _user_ubatch(cfg)
    want = _ctx_setting(cfg)
    step = 1024
    if want == "auto":
        top = AUTO_CONTEXTS[0]
        ctxs = [c for c in AUTO_CONTEXTS if c > 65536] + list(range(65536, 16383, -step))
    else:
        top = want
        floor = max(16384, (want // 2) // step * step)
        ctxs = [want] + [c for c in range((want - 1) // step * step, floor - 1, -step) if c < want]
    rungs: list[dict] = [{"label": f"{c:,}-token context, micro-batch {ub}", "ctx": c} for c in ctxs]
    if want != "auto":
        rungs[0]["label"] += " (your settings)"
    smallest = ctxs[-1]
    if int(s.get("n_cpu_moe", 38)) < 99:
        rungs.append({"label": f"all experts on CPU, {smallest:,}-token context, micro-batch {ub}",
                      "n_cpu_moe": 99, "ctx": smallest})
    if _supports(str(s.get("binary", "llama-server")), "--fit"):
        # last resort: no -ngl / --n-cpu-moe, so llama.cpp's own fitting decides what goes on the GPU
        rungs.append({"label": f"llama.cpp places the layers itself (--fit), {smallest:,}-token context, "
                               f"micro-batch {ub}", "auto_fit": True, "ctx": smallest})
    del top
    return rungs


MEMORY_ERRORS = ("out of memory", "failed to allocate", "cudamalloc", "cuda error", "ggml_abort", "vmm")


def _strip_flag(args: list[str], *names: str) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in names:
            skip = True
            continue
        out.append(a)
    return out


GIB = 1024 ** 3
RAM_WARNINGS: list[str] = []


def _mem_total() -> int:
    return int(oscompat.meminfo().get("MemTotal", 0))


def _apps_ram() -> int:
    """RAM your desktop and apps use right now, in bytes (0 when unknown). Not the page cache: a
    memory-mapped model shows up there, so system monitors leave it out of "used". Not llama-server
    either: its own buffers are planned anew when it starts."""
    mi = oscompat.meminfo()
    total, avail = int(mi.get("MemTotal", 0)), int(mi.get("MemAvailable", 0))
    if not total or not avail:
        return 0
    used = total - avail
    if oscompat.IS_LINUX:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                if Path(f"/proc/{entry}/comm").read_text().strip() != "llama-server":
                    continue
                for line in Path(f"/proc/{entry}/status").read_text().splitlines():
                    if line.startswith("RssAnon:"):
                        used -= int(line.split()[1]) * 1024
            except (OSError, ValueError, IndexError):
                continue
    return max(0, used)


@functools.lru_cache(maxsize=8)
def _supports(binary: str, flag: str) -> bool:
    """Whether this llama-server build knows *flag* (read once from --help)."""
    exe = shutil.which(binary)
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20)
        return flag in (r.stdout + r.stderr)
    except (OSError, subprocess.SubprocessError):
        return False


def _preload_guard(cfg: Config, extra: list[str]) -> list[str]:
    """Loading the model into RAM (--load-mode none/mlock, --no-mmap, --mlock) pins all of it, and the
    kernel can no longer drop model pages under pressure. Refuse that unless the RAM clearly has
    room for the model plus everything else; plain mmap loading is used instead."""
    s = cfg["server"]
    mode = extra[extra.index("--load-mode") + 1] if "--load-mode" in extra[:-1] else None
    pinned = mode in ("none", "mlock") or "--no-mmap" in extra or "--mlock" in extra
    if not pinned or s.get("allow_preload_low_ram"):
        return extra
    try:
        size = Path(s["model_path"]).expanduser().stat().st_size
    except OSError:
        return extra
    total = _mem_total()
    reserve = float(s.get("ram_reserve_gib", 12)) * GIB
    if not total or total >= size + reserve:
        return extra
    msg = (f"Not loading the model into RAM: it needs {size / GIB:.1f} GB pinned and this computer has "
           f"{total / GIB:.1f} GB, which leaves too little for everything else. Using memory-mapped loading "
           "instead (set server.allow_preload_low_ram: true to force it).")
    if msg not in RAM_WARNINGS:
        RAM_WARNINGS.append(msg)
    out = _strip_flag(extra, "--load-mode")
    return [a for a in out if a not in ("--no-mmap", "--mlock")]


def build_command(cfg: Config, rung: int = 0) -> list[str]:
    """The non-MTP llama-server command, with the fallback step *rung* applied on top."""
    s = cfg["server"]
    steps = ladder_for(cfg)
    step = steps[min(max(rung, 0), len(steps) - 1)]
    n_cpu_moe = max(int(s.get("n_cpu_moe", 38)), int(step.get("n_cpu_moe", 0)))
    ctx = int(step.get("ctx") or (64000 if _ctx_setting(cfg) == "auto" else _ctx_setting(cfg)))
    cmd = [
        s.get("binary", "llama-server"),
        "-m", str(Path(s["model_path"]).expanduser()),
        "--host", str(s.get("host", "127.0.0.1")),
        "--port", str(s.get("port", 8091)),
        *([] if step.get("auto_fit") else ["-ngl", str(s.get("gpu_layers", 99)), "--n-cpu-moe", str(n_cpu_moe)]),
        "-c", str(ctx),
        "-fa", str(s.get("flash_attn", "on")),
        "-ctk", str(s.get("cache_type_k", "q8_0")),
        "-ctv", str(s.get("cache_type_v", "q8_0")),
        "-t", str(s.get("threads", 8)),
        "--jinja",  # native tool calling through the model's chat template
    ]
    extra = [str(a) for a in (s.get("extra_args") or [])]
    banned = {"--spec-type", "--spec-draft-n-max", "--model-draft", "-md"}
    if banned & set(extra):
        raise ValueError(f"server.extra_args contains speculative/MTP flags ({', '.join(banned & set(extra))}); "
                         "remove them — this config is the non-MTP baseline.")
    if "ub" in step:
        extra = _strip_flag(extra, "-ub", "--ubatch-size") + ["-ub", str(step["ub"])]
    if step.get("auto_fit"):
        extra = _strip_flag(extra, "-ngl", "--gpu-layers", "--n-gpu-layers", "--n-cpu-moe", "-ncmoe", "--fit") + ["--fit", "on"]
    elif not ({"--fit", "-fit"} & set(extra)) and _supports(str(s.get("binary", "llama-server")), "--fit"):
        # Hermes Lite sets the layer split itself. llama.cpp's pre-check would then keep ~1 GiB of the
        # GPU free as a margin (a quarter of a 4 GB card) and refuse settings that do fit: let the real
        # allocation decide; if memory truly runs out, the fallback steps take over.
        extra += ["--fit", "off"]
    extra = _preload_guard(cfg, extra)
    # One conversation at a time: a single slot keeps the whole cache in one place (with several,
    # a request can land on a slot that doesn't hold the system prompt and read it all again).
    par = s.get("parallel", 1)
    if not ({"-np", "--parallel"} & set(extra)) and str(par).lower() not in ("auto", "0", "none", ""):
        extra += ["-np", str(int(par))]
    if "--slot-save-path" not in extra and _supports(str(s.get("binary", "llama-server")), "--slot-save-path"):
        slot_dir = home() / "slot-cache"
        slot_dir.mkdir(parents=True, exist_ok=True)
        extra += ["--slot-save-path", str(slot_dir)]
    # RAM budget: the model must stay in RAM for fast prefill, so the optional extras (prompt cache in
    # RAM, a CPU-side vision encoder) only get what is left after the model and the desktop.
    cram, mm, mm_on_gpu = ram_plan(cfg)
    if "--cache-ram" not in extra and "-cram" not in extra \
            and _supports(str(s.get("binary", "llama-server")), "--cache-ram"):
        extra += ["--cache-ram", str(int(cram))]
    if mm and "--mmproj" not in extra and "-mm" not in extra:
        extra += ["--mmproj", str(mm)]
        if not mm_on_gpu:
            extra += ["--no-mmproj-offload"]
        if s.get("image_max_tokens") and _supports(str(s.get("binary", "llama-server")), "--image-max-tokens"):
            extra += ["--image-max-tokens", str(int(s["image_max_tokens"]))]
    return cmd + extra


def _warn(msg: str) -> None:
    if msg not in RAM_WARNINGS:
        RAM_WARNINGS.append(msg)


ENCODER_WORK = GIB // 2   # rough: the encoder's working memory for one image, on top of its file
RAM_MARGIN = GIB // 2


def ram_budget(cfg: Config) -> dict | None:
    """What the RAM holds: total, your apps (measured now, at least server.ram_desktop_gib), the model
    and what is left for the extras. None when unknown (no model file or no /proc/meminfo)."""
    s = cfg["server"]
    total = _mem_total()
    try:
        model = Path(s["model_path"]).expanduser().stat().st_size
    except OSError:
        return None
    if not total:
        return None
    apps = max(_apps_ram(), int(float(s.get("ram_desktop_gib", 5)) * GIB))
    return {"total": total, "apps": apps, "model": model, "spare": total - apps - model - RAM_MARGIN}


def ram_plan(cfg: Config) -> tuple[int, Path | None, bool]:
    """(prompt-cache MiB, vision encoder file or None, encoder on GPU?) that fit this computer's RAM.
    The model must stay in RAM: when it does not fit, Linux drops parts of it and reads them back from
    disk on every prompt, and prompt reading stalls. So the extras only get what is really left."""
    s = cfg["server"]
    want_cram = int(s.get("cache_ram_mib", 512) or 0)
    mm = find_mmproj(cfg)
    on_gpu = vision_device(cfg) == "vram"
    b = ram_budget(cfg)
    if not b:
        return want_cram, mm, on_gpu
    spare, g = b["spare"], (lambda n: f"{n / GIB:.1f} GB")
    if spare < 0:
        _warn(f"The model ({g(b['model'])}) does not fit in the RAM your apps leave free ({g(b['apps'])} of "
              f"{g(b['total'])} in use): Linux will drop parts of it and read them back from disk, so prompt "
              "reading can stall for minutes. Close apps or use a smaller quant.")
    need_mm = (mm.stat().st_size + ENCODER_WORK) if (mm and not on_gpu) else 0
    if need_mm and spare - need_mm < 0:
        # in RAM it would push the model out (stalls); on the GPU it would cost context: neither silently
        _warn(f"Vision is off for this run: the encoder needs about {g(need_mm)} of RAM and only "
              f"{g(max(spare, 0))} is left after the model ({g(b['model'])}) and your apps ({g(b['apps'])}). "
              "Close apps, then /vision ram again — or /vision vram (costs context), or a smaller model.")
        mm, need_mm = None, 0
    cram = max(0, min(want_cram, int((spare - need_mm) / (1024 ** 2))))
    if cram < want_cram:
        _warn(f"Prompt cache in RAM lowered to {cram} MB (from {want_cram}) so the model stays in RAM.")
    return cram, mm, on_gpu


def vision_device(cfg: Config) -> str:
    """server.mmproj_device: "vram" (GPU, the default) or "ram" (CPU)."""
    d = str(cfg["server"].get("mmproj_device", "vram") or "vram").strip().lower()
    return "ram" if d in ("ram", "cpu") else "vram"


def find_mmproj(cfg: Config) -> Path | None:
    """server.mmproj: a path, "auto" (an mmproj*.gguf next to the model) or "none"."""
    s = cfg["server"]
    setting = str(s.get("mmproj", "none") or "none").strip()
    if setting.lower() in ("none", "off", "false", ""):
        return None
    if setting.lower() != "auto":
        p = Path(setting).expanduser()
        return p if p.is_file() else None
    folder = Path(s["model_path"]).expanduser().parent
    try:
        found = sorted(folder.glob("*mmproj*.gguf"), key=lambda p: ("bf16" not in p.name.lower()
                                                                   and "f16" not in p.name.lower(), p.name))
    except OSError:
        return None
    return found[0] if found else None


def _terminal_argv(choice: str, title: str, script: Path) -> list[str]:
    run = ["bash", str(script)]
    known = {
        "kitty": lambda b: [b, "--title", title, "--class", "hermes-lite-server", *run],
        "alacritty": lambda b: [b, "--title", title, "--class", "hermes-lite-server", "-e", *run],
        "foot": lambda b: [b, "--title", title, "--app-id", "hermes-lite-server", *run],
        "wezterm": lambda b: [b, "start", "--class", "hermes-lite-server", "--", *run],
        "ghostty": lambda b: [b, f"--title={title}", "-e", *run],
        "konsole": lambda b: [b, "-p", f"tabtitle={title}", "-e", *run],
        "xterm": lambda b: [b, "-T", title, "-e", *run],
    }
    candidates = [choice] if choice and choice != "auto" else []
    if not candidates:
        if os.environ.get("TERMINAL"):
            candidates.append(os.environ["TERMINAL"])
        candidates += list(known)
    for cand in candidates:
        binary = shutil.which(cand.split()[0])
        if not binary:
            continue
        name = Path(binary).name
        return known[name](binary) if name in known else [binary, "-e", *run]
    raise RuntimeError("No terminal emulator found for the llama-server window. Set server.terminal in config.yaml.")


class LlamaServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        s = cfg["server"]
        self.base = f"http://{s.get('host', '127.0.0.1')}:{s.get('port', 8091)}"
        logs = home() / "logs"
        self.pidfile = logs / "llama-server.pid"
        self.logfile = logs / "llama-server.log"
        self.stopflag = logs / "llama-server.stopping"
        self.script = logs / "run-llama-server.sh"
        self.tuning_file = logs / "server-tuning.json"
        self.exitfile = logs / "llama-server.exit"
        self.ownerfile = logs / "run-llama-server.owner"   # PID of the launch script that owns these files
        self._launches = 0
        self.owned = False
        self.status = "down"  # down | starting | loading | tuning | ready | error
        self.error = ""
        self.ladder = ladder_for(cfg)
        self.trusted, self.saved_on = False, ""
        self.rung = self._saved_rung()
        self._apply_rung_context()

    # ── fallback ladder ──────────────────────────────────────
    def _signature(self) -> str:
        """What the saved setting depends on: your memory-relevant settings, not the exact command
        (a Hermes Lite update that adds a flag must not throw away a setting that works)."""
        s = self.cfg["server"]
        keys = ("binary", "model_path", "gpu_layers", "n_cpu_moe", "ctx_size", "flash_attn", "cache_type_k",
                "cache_type_v", "extra_args")
        sig = {k: s.get(k) for k in keys}
        if (mm := find_mmproj(self.cfg)) and ram_plan(self.cfg)[2]:
            sig["vision_on_gpu"] = str(mm)  # an encoder in VRAM changes what fits: test again
        return json.dumps(sig, sort_keys=True, default=str)

    @staticmethod
    def _step_key(step: dict) -> dict:
        return {k: step.get(k) for k in ("ctx", "ub", "n_cpu_moe")}

    def _saved_rung(self) -> int:
        """The setting that last worked with these settings, so launches go straight to it."""
        self.trusted = False
        try:
            data = json.loads(self.tuning_file.read_text())
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        old_format = "version" not in data  # written by an older Hermes Lite: signature was the full command
        if not old_format and data.get("signature") != self._signature():
            return 0  # you changed a memory setting in config.yaml: search again
        keys = [self._step_key(st) for st in self.ladder]
        if data.get("step") in keys:
            idx = keys.index(data["step"])
        elif data.get("label") in [st["label"] for st in self.ladder]:
            idx = [st["label"] for st in self.ladder].index(data["label"])
        else:
            return 0
        self.trusted = True
        self.saved_on = data.get("verified", "")
        return idx

    def _save_rung(self) -> None:
        self.saved_on = time.strftime("%Y-%m-%d %H:%M")
        try:
            self.tuning_file.write_text(json.dumps({
                "version": 2, "signature": self._signature(), "rung": self.rung,
                "label": self.ladder[self.rung]["label"], "step": self._step_key(self.ladder[self.rung]),
                "verified": self.saved_on}, indent=1))
        except OSError:
            pass

    def _apply_rung_context(self) -> None:
        """Keep the agent's context window in step with the server's -c."""
        step = self.ladder[self.rung]
        setting = _ctx_setting(self.cfg)
        ctx = int(step.get("ctx") or (64000 if setting == "auto" else setting))
        self.cfg["model"]["context_length"] = ctx  # the server's -c is the truth

    def rung_label(self) -> str:
        return self.ladder[self.rung]["label"]

    def gpu_competitors(self) -> str:
        """A warning when another program holds GPU memory (another llama-server, a game, Tiny Code…)."""
        mine = self.pid()
        others = [(p, n, mb) for p, n, mb in oscompat.gpu_users() if p != mine and mb >= 300]
        if not others:
            return ""
        what = ", ".join(f"{Path(n).name} (process {p}, {mb} MB)" for p, n, mb in others)
        return (f"Another program is using GPU memory: {what}. With 4 GB, two models can't share the GPU, so "
                "llama-server may crash or run out of memory. Close it (for another llama-server: stop that "
                "tool's backend) and use /server restart.")

    def _memory_crash(self) -> bool:
        tail = self.log_tail(80).lower()
        if any(marker in tail for marker in MEMORY_ERRORS):
            return True
        # When a GPU allocation fails, llama.cpp aborts at once and its last log lines (the error) are
        # often lost: its log is written by a background thread that never gets to flush. So a death
        # before "model loaded" with no error in the log, or by abort/kill, is treated the same way.
        try:
            code = int(self.exitfile.read_text().strip())
        except (OSError, ValueError):
            code = None
        loaded = "model loaded" in tail or "listening on" in tail
        logged_error = any(f" {lvl} " in line for line in tail.splitlines()[-15:] for lvl in ("e", "f")) or \
            "error" in "\n".join(tail.splitlines()[-5:])
        return not loaded and (code in (134, 137, 139, -6, -9, -11) or not logged_error)

    # ── health ───────────────────────────────────────────────
    async def health(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{self.base}/health")
            if r.status_code == 200:
                return "ready"
            if r.status_code == 503:
                return "loading"
            return "error"
        except httpx.HTTPError:
            return "down"

    def pid(self) -> int | None:
        try:
            pid = int(self.pidfile.read_text().strip())
            return pid if oscompat.pid_alive(pid) else None
        except (OSError, ValueError):
            return None

    def log_tail(self, lines: int = 25) -> str:
        try:
            return "\n".join(self.logfile.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    # ── launch ───────────────────────────────────────────────
    def _write_script(self) -> None:
        cmd = build_command(self.cfg, self.rung)
        quoted = " ".join(shlex.quote(c) for c in cmd)
        own = shlex.quote(str(self.ownerfile))
        self.script.write_text(f"""#!/usr/bin/env bash
echo $$ > {own}
printf '\\033]0;llama-server · Hermes Lite\\007'
rm -f {shlex.quote(str(self.stopflag))} {shlex.quote(str(self.exitfile))}
echo "☤ Hermes Lite — llama-server"
echo {shlex.quote("$ " + quoted)}
echo
bash -c 'echo $$ > {shlex.quote(str(self.pidfile))}; exec "$@"' _ {quoted} 2>&1 | tee {shlex.quote(str(self.logfile))}
code=${{PIPESTATUS[0]}}
# a newer launch owns the pid/exit files now (a restart): leave them alone
[ "$(cat {own} 2>/dev/null)" = "$$" ] || exit 0
rm -f {shlex.quote(str(self.pidfile))}
echo "$code" > {shlex.quote(str(self.exitfile))}
if [ -e {shlex.quote(str(self.stopflag))} ]; then rm -f {shlex.quote(str(self.stopflag))}; exit 0; fi
echo
echo "llama-server exited with code $code. Press Enter to close this window."
read -r _
""", encoding="utf-8")
        self.script.chmod(0o755)

    def _wait_old_script(self) -> None:
        """The previous launch script finishes after its llama-server exits (it records the exit code).
        Starting the next one before that let the old script delete the new server's pid file and
        report it dead: a restart then stopped instead. Wait for it (3 s at most), then end it."""
        try:
            old = int(self.ownerfile.read_text().strip())
        except (OSError, ValueError):
            return
        for _ in range(60):
            if not oscompat.pid_alive(old):
                return
            time.sleep(0.05)
        if self.pid() is None:   # its server is gone: it is only waiting at "Press Enter"
            oscompat.terminate(old)

    def launch(self) -> None:
        s = self.cfg["server"]
        model = Path(s["model_path"]).expanduser()
        if not model.exists():
            raise FileNotFoundError(f"Model not found: {model}  (set server.model_path in {self.cfg['_path']})")
        if not shutil.which(s.get("binary", "llama-server")):
            raise FileNotFoundError(f"'{s.get('binary')}' is not on PATH (set server.binary in {self.cfg['_path']})")
        self._wait_old_script()
        self._launches += 1
        self.logfile.write_text("")
        self.exitfile.unlink(missing_ok=True)
        if not oscompat.IS_LINUX:
            self._launch_direct()
            return
        self._write_script()
        if str(s.get("terminal", "auto")).lower() in ("none", "headless", "off"):
            # No window: the server runs in the background; its output goes to the log file
            # (/server in Hermes Lite shows the tail, or: tail -f ~/.hermes-lite/logs/llama-server.log).
            subprocess.Popen(["bash", str(self.script)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            self.owned = True
            self.status = "starting"
            return
        # a plain window, placed by your window manager (class / app id "hermes-lite-server" where the terminal allows)
        argv = _terminal_argv(s.get("terminal", "auto"), "llama-server", self.script)
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        self.owned = True
        self.status = "starting"

    def _launch_direct(self) -> None:
        """macOS and Windows: start llama-server itself (no bash script, no terminal window). Its
        output goes to the log file; /server shows the tail. On Windows,
        server.terminal: window opens it in its own console window instead (no log then)."""
        cmd = build_command(self.cfg, self.rung)
        window = oscompat.IS_WINDOWS and str(self.cfg.get_path("server.terminal", "auto")).lower() == "window"
        self.stopflag.unlink(missing_ok=True)
        log = None if window else open(self.logfile, "ab")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log or None,
                                stderr=subprocess.STDOUT if log else None, **oscompat.new_group_kwargs(window=window))
        if log:
            log.close()
        self.pidfile.write_text(str(proc.pid))

        launch = self._launches

        def watch() -> None:  # what the bash script does on Linux: record the exit code
            code = proc.wait()
            if launch != self._launches:   # restarted since: these files belong to the new server
                return
            self.pidfile.unlink(missing_ok=True)
            self.exitfile.write_text(str(code))
            self.stopflag.unlink(missing_ok=True)

        import threading
        threading.Thread(target=watch, daemon=True, name="llama-server-watch").start()
        self.owned = True
        self.status = "starting"

    async def _wait_loaded(self, report) -> str:
        """Wait for /health. Returns ready | died | timeout."""
        deadline = time.monotonic() + float(self.cfg.get_path("server.health_timeout", 600))
        seen_pid = False
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            state = await self.health()
            if state == "ready":
                return "ready"
            pid = self.pid()
            seen_pid = seen_pid or pid is not None
            if self.exitfile.exists() or (seen_pid and pid is None):
                return "died"
            report("loading" if state == "loading" or pid else "starting", "")
        return "timeout"

    async def warmup(self, body: dict) -> bool:
        """Send a real, full-size prompt (system prompt + tools) and generate one token.
        This is where a too-tight GPU setup crashes, so it is tested before you type anything.
        It also leaves the system prompt in the server's cache, so your first message starts faster."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=900, write=60, pool=10)) as c:
                await c.post(f"{self.base}/v1/chat/completions", json=body)
            return True
        except httpx.HTTPError:
            await asyncio.sleep(1.0)
            return self.pid() is not None and await self.health() == "ready"

    # ── system prompt cache kept on disk between launches ─────────────────
    def _slot_dir(self) -> Path:
        return home() / "slot-cache"

    async def _slot(self, action: str, filename: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=60, pool=10)) as c:
                r = await c.post(f"{self.base}/slots/0?action={action}", json={"filename": filename})
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def warm(self, body: dict, report, label: str) -> bool:
        """Get the system prompt + tool list into llama-server's cache. Read once, then saved to disk
        (--slot-save-path): the next launch with the same prompt restores it in seconds instead of
        reading ~10k tokens again. An untested setting is always tested with the real prompt."""
        cmd = build_command(self.cfg, self.rung)
        key = hashlib.sha256(json.dumps([cmd, body], sort_keys=True, default=str).encode()).hexdigest()[:20]
        fname = f"warm-{key}.bin"
        can_save = "--slot-save-path" in cmd
        if self.trusted and can_save and (self._slot_dir() / fname).is_file():
            report("tuning", "restoring the system prompt saved at the last launch")
            if await self._slot("restore", fname):
                return True
        report("tuning", f"{'reading the system prompt (saved for the next launch)' if self.trusted else 'testing a full-size prompt'}: {label}")
        if not await self.warmup({**body, "id_slot": 0}):
            return False
        if can_save and await self._slot("save", fname):
            saved = sorted(self._slot_dir().glob("warm-*.bin"), key=lambda p: p.stat().st_mtime)
            for old in saved[:-4]:  # keep the last few prompts (projects, days)
                old.unlink(missing_ok=True)
        return True

    async def _stop_and_wait(self) -> None:
        pid = self.pid()
        if pid:
            self.stopflag.touch()
            oscompat.terminate(pid)
            for _ in range(100):
                if self.pid() is None:
                    break
                await asyncio.sleep(0.1)
        for _ in range(50):  # port free?
            if await self.health() == "down":
                break
            await asyncio.sleep(0.2)

    async def ensure(self, on_status=None, warmup_body=None) -> bool:
        """Reuse a running server, or launch one and step down the fallback ladder until a
        full-size prompt runs without the GPU running out of memory."""
        def report(state: str, detail: str = ""):
            if state != "warning":
                self.status = state
            if on_status:
                on_status(state, detail)

        if self.other_ports():  # make room on the GPU: one model at a time (server.stop_others)
            stopped = await self.take_over(report)
            if stopped:
                report("warning", "Stopped to make room on the GPU: " + ", ".join(stopped) + ". Quitting Hermes "
                       "Lite (or /server handoff) starts it again exactly as it was.")
        if await self.health() == "ready":
            running = await self.served_model()
            want = Path(self.cfg["server"]["model_path"]).expanduser()
            if running and Path(running).name != want.name and self.cfg.get_path("server.autostart", True):
                # A server Hermes Lite didn't start, with another model (an old test, another tool):
                # replace it, or every reply comes from the wrong model.
                report("starting", f"llama-server on port {self.cfg['server'].get('port', 8091)} runs "
                                    f"{Path(running).name}, not {want.name}: stopping it and starting yours")
                await self.stop_any()
            else:
                ctx = await self.served_context()
                if ctx:  # its -c, not ours: compaction and the context bar must use what it really has
                    self.cfg["model"]["context_length"] = ctx
                    if ctx < MIN_CONTEXT:
                        report("warning", f"The llama-server on port {self.cfg['server'].get('port')} was started by "
                                          f"another program with only {ctx:,} tokens of context; Hermes Lite's prompt "
                                          f"and tools alone take ~9k. /server restart replaces it with Hermes Lite's "
                                          f"settings ({self.cfg['server'].get('ctx_size')} context) on the same port, "
                                          "for both programs to use.")
                report("ready", "already running" + (f" ({Path(running).name}" + (f", {ctx // 1000}k context"
                                                                                   if ctx else "") + ")" if running else ""))
                return True
        if not self.cfg.get_path("server.autostart", True):
            report("down", "autostart is off and nothing answers at " + self.base)
            return False
        retried = False
        while self.rung < len(self.ladder):
            self._apply_rung_context()
            label = f"{self.rung_label()} ({self.rung + 1}/{len(self.ladder)})"
            await self._stop_and_wait()
            try:
                self.launch()
            except Exception as exc:  # missing model / binary / terminal: no point retrying
                self.error = str(exc)
                report("error", str(exc))
                return False
            report("starting", f"starting llama-server: {label}")
            if busy := self.gpu_competitors():
                report("warning", busy)
            outcome = await self._wait_loaded(report)
            if outcome == "timeout":
                self.error = "Timed out waiting for llama-server to load the model."
                report("error", self.error)
                return False
            # The full-size prompt also primes llama-server's cache with the system prompt and tool
            # list, so your first message doesn't wait for them. A saved setting is not re-searched.
            if outcome == "ready" and warmup_body is not None:
                if not await self.warm(warmup_body, report, label):
                    outcome = "died"
            if outcome == "ready":
                if not self.trusted:
                    self._save_rung()
                    self.trusted = True
                report("ready", f"model loaded ({self.rung_label()})")
                return True
            if not self._memory_crash():
                self.error = "llama-server exited:\n" + self.log_tail()
                report("error", self.error)
                return False
            if self.trusted and not retried:
                # It worked before: the GPU may just be busy right now (a browser, a game). Try the
                # same setting once more before settling for less.
                retried = True
                report("tuning", f"out of GPU memory with {self.rung_label()}, which worked before — retrying once")
                await asyncio.sleep(3)
                continue
            report("tuning", f"out of GPU memory with {self.rung_label()} — trying 1k less context")
            self.trusted, retried = False, False
            self.rung += 1
        self.rung = len(self.ladder) - 1
        try:
            code = self.exitfile.read_text().strip()
        except OSError:
            code = "?"
        self.error = (f"llama-server would not start even with a {self.ladder[-1].get('ctx', 0):,}-token context "
                      f"(last exit code {code}). Either something else is using the GPU (check nvidia-smi), or "
                      f"this model file is the problem rather than its size: try it once by hand with "
                      f"hermes-lite --print-server-cmd, or go back to the previous model.\n" + self.log_tail())
        report("error", self.error)
        return False

    def step_down(self) -> bool:
        """After a crash mid-session: move to the next safer setting. False when none is left."""
        if self.rung + 1 >= len(self.ladder):
            return False
        self.rung += 1
        self.trusted = False  # verified (and saved) by the next ensure()
        return True

    async def served_context(self) -> int | None:
        """The context per request of the running server (its -c divided among its slots)."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                props = (await client.get(f"{self.base}/props")).json()
            n = (props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx")
            return int(n) if n else None
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None

    async def served_model(self) -> str | None:
        """The model file the running server actually loaded (from /props)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.base}/props")
            return (r.json() or {}).get("model_path") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def stop_any(self) -> list[int]:
        """Stop whatever llama-server listens on our port, started by Hermes Lite or not."""
        killed = []
        pid = self.pid()
        if pid:
            self.stopflag.touch()
            oscompat.terminate(pid)
            killed.append(pid)
        for p in oscompat.pids_on_port(int(self.cfg["server"].get("port", 8091))):
            if p not in killed and "llama" in oscompat.process_cmdline(p).lower():
                oscompat.terminate(p)
                killed.append(p)
        port = int(self.cfg["server"].get("port", 8091))

        def gone() -> bool:
            return not any(oscompat.pid_alive(p) for p in killed) and not oscompat.pids_on_port(port)

        for _ in range(80):  # 8 s to exit on its own (it may be in the middle of a long prompt)
            if gone():
                break
            await asyncio.sleep(0.1)
        else:
            for p in killed + [p for p in oscompat.pids_on_port(port) if p not in killed]:
                oscompat.force_kill(p)
            for _ in range(50):
                if gone():
                    break
                await asyncio.sleep(0.1)
        self.owned = False
        self.status = "down"
        return killed

    # ── a server Hermes Lite did not start (e.g. Tiny Code Agent's on 8080) ──
    def external_info(self) -> dict | None:
        """The llama process listening on our port when Hermes Lite did not start it: its pid,
        command line, working folder and environment, so /server restart can start it again
        exactly as it was. None when there is none, or Hermes Lite started it."""
        port = int(self.cfg["server"].get("port", 8091))
        own = self.pid()
        for pid in oscompat.pids_on_port(port):
            if pid == own or "llama" not in oscompat.process_cmdline(pid).lower():
                continue
            return process_info(pid)
        return None

    # ── handing the GPU back and forth with another program's server ──
    def other_ports(self) -> list[int]:
        own = int(self.cfg["server"].get("port", 8091))
        out = []
        for p in self.cfg.get_path("server.stop_others", []) or []:
            try:
                if int(p) != own:
                    out.append(int(p))
            except (TypeError, ValueError):
                continue
        return out

    @property
    def stopped_file(self) -> Path:
        return self.logfile.parent / "stopped-servers.json"

    def stopped(self) -> dict:
        try:
            return json.loads(self.stopped_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_stopped(self, data: dict) -> None:
        if not data:
            self.stopped_file.unlink(missing_ok=True)
            return
        self.stopped_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
        try:
            self.stopped_file.chmod(0o600)  # it holds that program's environment
        except OSError:
            pass

    async def take_over(self, report=None) -> list[str]:
        """Stop the llama servers on server.stop_others (e.g. Tiny Code Agent's on 8080), after
        recording exactly how they were started, so give_back() can start them again unchanged."""
        report = report or (lambda state, detail="": None)
        done, saved = [], self.stopped()
        for port in self.other_ports():
            pids = [p for p in oscompat.pids_on_port(port) if "llama" in oscompat.process_cmdline(p).lower()]
            for pid in pids:
                info = process_info(pid)
                if info.get("argv"):
                    saved[str(port)] = {**info, "stopped": time.strftime("%Y-%m-%d %H:%M")}
                    self._save_stopped(saved)
                report("starting", f"making room: stopping the llama-server on port {port} (PID {pid})")
                await _stop_pids([pid], port)
                done.append(f"port {port} (PID {pid})")
        return done

    def give_back(self) -> list[str]:
        """Start the servers take_over() stopped, exactly as they were, in the background (they keep
        running after Hermes Lite exits). Returns what was started."""
        started, saved = [], self.stopped()
        for port, info in list(saved.items()):
            if oscompat.pids_on_port(int(port)):
                saved.pop(port)  # something is already there again
                continue
            log = open(self.logfile.parent / f"restored-{port}.log", "ab")
            try:
                proc = subprocess.Popen(info["argv"], cwd=info.get("cwd") or None, env=info.get("env") or None,
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        **oscompat.new_group_kwargs())
                started.append(f"port {port} (PID {proc.pid})")
                saved.pop(port)
            except OSError as exc:
                started.append(f"port {port}: could not start ({exc})")
            finally:
                log.close()
        self._save_stopped(saved)
        return started

    async def hand_over(self) -> list[str]:
        """Free the GPU for the other program: stop our server, then start theirs again."""
        own = self.pid()
        port = int(self.cfg["server"].get("port", 8091))
        pids = [p for p in ([own] if own else []) + oscompat.pids_on_port(port)
                if "llama" in oscompat.process_cmdline(p).lower()]
        if own:
            self.stopflag.touch()
        await _stop_pids(sorted(set(pids)), port)
        self.owned, self.status = False, "handed over"
        return self.give_back()

    async def restart_external(self, info: dict, report=None) -> bool:
        """Force-stop that server and start the same command again (same binary, flags, folder and
        environment), in the background so it outlives Hermes Lite like the original. Its output
        goes to Hermes Lite's log, so /server and the progress line work. Hermes Lite still does
        not own it: quitting Hermes Lite leaves it running."""
        report = report or (lambda state, detail="": None)
        if not info.get("argv"):
            report("error", "Could not read that server's command line, so it cannot be started again as it was.")
            return False
        report("starting", f"stopping llama-server (PID {info['pid']})")
        await self.stop_any()
        self.logfile.write_text("")
        log = open(self.logfile, "ab")
        try:
            self._external = subprocess.Popen(info["argv"], cwd=info.get("cwd") or None, env=info.get("env") or None,
                                              stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                              **oscompat.new_group_kwargs())
        except OSError as exc:
            report("error", f"Could not start it again: {exc}")
            return False
        finally:
            log.close()
        report("starting", f"started again (PID {self._external.pid}) with its own settings; loading the model")
        deadline = time.monotonic() + float(self.cfg.get_path("server.health_timeout", 600))
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if self._external.poll() is not None:
                report("error", f"It exited (code {self._external.returncode}):\n" + self.log_tail())
                return False
            state = await self.health()
            if state == "ready":
                self.owned = False
                report("ready", "restarted with its own settings")
                return True
            report("loading", "")
        report("error", "Timed out waiting for it to load the model.")
        return False

    def stop(self) -> None:
        if not self.owned:
            return
        pid = self.pid()
        if pid:
            self.stopflag.touch()
            oscompat.terminate(pid)
        self.owned = False
        self.status = "down"


MIN_CONTEXT = 16384  # below this, Hermes Lite cannot work (prompt + tools + one reply)


def flag_table(argv: list[str]) -> dict[str, str]:
    """{flag: value} of a llama-server command line ('(on)' for switches)."""
    out, i = {}, 1
    while i < len(argv):
        a = argv[i]
        if not a.startswith("-"):
            i += 1
            continue
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if nxt is not None and not (nxt.startswith("-") and not nxt[1:].replace(".", "", 1).isdigit()):
            out[a], i = nxt, i + 2
        else:
            out[a], i = "(on)", i + 1
    return out


def compare_commands(theirs: list[str], mine: list[str]) -> list[tuple[str, str, str]]:
    """(flag, running server's value, Hermes Lite's value) for every flag that differs."""
    a, b = flag_table(theirs), flag_table(mine)
    show = lambda v: Path(v).name if "/" in v else v
    return [(k, show(a.get(k, "—")), show(b.get(k, "—"))) for k in sorted(set(a) | set(b))
            if a.get(k) != b.get(k) and k not in ("--port", "--host")]


def process_info(pid: int) -> dict:
    """pid, command line (with the binary's absolute path), working folder and environment of a
    process, enough to start the same thing again later."""
    argv, cwd, env = [], None, None
    try:
        if oscompat.IS_LINUX:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
            argv = argv[:-1] if argv and argv[-1] == "" else argv  # cmdline ends with a NUL
            cwd = os.readlink(f"/proc/{pid}/cwd")
            raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
            env = dict(x.split("=", 1) for x in raw if "=" in x)
            exe = os.readlink(f"/proc/{pid}/exe")
            if argv and os.path.isfile(exe) and not exe.endswith(" (deleted)") and Path(exe).name == Path(argv[0]).name:
                argv[0] = exe  # absolute path: works whatever PATH and folder we start it from
        else:
            import psutil
            proc = psutil.Process(pid)
            argv, cwd, env = proc.cmdline(), proc.cwd(), proc.environ()
    except Exception:
        pass
    return {"pid": pid, "argv": argv, "cwd": cwd, "env": env}


async def _stop_pids(pids: list[int], port: int) -> None:
    """Ask, wait up to 8 s (a server in the middle of a long prompt takes a while), then force."""
    for p in pids:
        oscompat.terminate(p)

    def gone() -> bool:
        return not any(oscompat.pid_alive(p) for p in pids) and not oscompat.pids_on_port(port)

    for _ in range(80):
        if gone():
            return
        await asyncio.sleep(0.1)
    for p in pids + [p for p in oscompat.pids_on_port(port) if p not in pids]:
        oscompat.force_kill(p)
    for _ in range(50):
        if gone():
            return
        await asyncio.sleep(0.1)
