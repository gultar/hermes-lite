"""What differs between Linux, macOS and Windows, in one place.

Linux keeps the original code paths (/proc, process groups, bash). macOS uses bash too, with psutil
for memory and processes. Windows uses PowerShell for the terminal tool, taskkill for process trees,
psutil for memory, and runs llama-server without a bash script."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def os_name() -> str:
    return "Windows" if IS_WINDOWS else "macOS" if IS_MAC else "Linux"


# ── shell ────────────────────────────────────────────────────────────────
def powershell() -> str | None:
    """PowerShell 7 (pwsh) when installed, else Windows PowerShell 5.1."""
    return shutil.which("pwsh") or shutil.which("powershell")


def shell_name() -> str:
    if IS_WINDOWS:
        exe = powershell() or ""
        return "PowerShell 7 (pwsh)" if exe.lower().endswith(("pwsh", "pwsh.exe")) else "Windows PowerShell"
    return "bash"


def new_group_kwargs(window: bool = False) -> dict:
    """Popen arguments that put the child in its own process group, so its whole tree can be stopped."""
    if IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        flags |= subprocess.CREATE_NEW_CONSOLE if window else subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


def kill_tree(pid: int, grace: float = 0.5) -> None:
    """Stop a process and everything it started (asks first, then forces)."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, timeout=10)
        time.sleep(grace)
        if pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        time.sleep(grace)
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def terminate(pid: int) -> None:
    """Ask a process (and, on Windows, its tree) to exit."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def force_kill(pid: int) -> None:
    """Stop a process that ignored the polite request."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        # os.kill(pid, 0) on Windows would send Ctrl+C to the process group: never use it here
        try:
            import psutil
            return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except ImportError:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    if IS_LINUX:
        try:  # a zombie (exited, not yet collected) is not alive
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat[stat.rindex(")") + 2:][:1] != "Z"
        except (OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── memory and processes ─────────────────────────────────────────────────
def meminfo() -> dict[str, int]:
    """At least MemTotal and MemAvailable, in bytes ({} when unknown)."""
    if IS_LINUX:
        out: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    out[key] = int(parts[0]) * 1024
        except (OSError, ValueError):
            pass
        return out
    try:
        import psutil
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {"MemTotal": vm.total, "MemAvailable": vm.available, "MemFree": getattr(vm, "free", 0),
                "SwapTotal": sw.total, "SwapFree": sw.free}
    except Exception:
        return {}


def process_table() -> dict[int, tuple[int, int, int]]:
    """pid -> (ppid, process group or 0, rss bytes)."""
    if IS_LINUX:
        page = os.sysconf("SC_PAGE_SIZE")
        table: dict[int, tuple[int, int, int]] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
                fields = stat[stat.rindex(")") + 2:].split()
                rss = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * page
                table[pid] = (int(fields[1]), int(fields[2]), rss)
            except (OSError, ValueError, IndexError):
                continue
        return table
    try:
        import psutil
    except ImportError:
        return {}
    table = {}
    for p in psutil.process_iter(["pid", "ppid", "memory_info"]):
        try:
            mi = p.info.get("memory_info")
            pgid = 0 if IS_WINDOWS else os.getpgid(p.info["pid"])
            table[p.info["pid"]] = (p.info.get("ppid") or 0, pgid, mi.rss if mi else 0)
        except Exception:
            continue
    return table


def process_cmdline(pid: int) -> str:
    if IS_LINUX:
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            return ""
    try:
        import psutil
        return " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return ""


def memory_supported() -> bool:
    return bool(meminfo().get("MemTotal"))


# ── places ───────────────────────────────────────────────────────────────
def data_dir() -> Path:
    """Where the venv and saved versions live (install.sh / install.ps1 use the same place)."""
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "hermes-lite"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "hermes-lite"


def install_hint(tool: str) -> str:
    if IS_WINDOWS:
        return f"winget or scoop (e.g. scoop install {tool})"
    if IS_MAC:
        return f"brew install {tool}"
    return f"your package manager (e.g. sudo pacman -S {tool})"


def pids_on_port(port: int) -> list[int]:
    """Processes listening on a local TCP port (to find a llama-server Hermes Lite did not start)."""
    pids: set[int] = set()
    if IS_LINUX and shutil.which("ss"):
        try:
            out = subprocess.run(["ss", "-Hltnp", f"sport = :{port}"], capture_output=True, text=True,
                                 timeout=5).stdout
            import re
            pids |= {int(x) for x in re.findall(r"pid=(\d+)", out)}
        except (OSError, subprocess.SubprocessError):
            pass
    if not pids and shutil.which("lsof") and not IS_WINDOWS:
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True,
                                 timeout=5).stdout
            pids |= {int(x) for x in out.split() if x.isdigit()}
        except (OSError, subprocess.SubprocessError):
            pass
    if not pids:
        try:
            import psutil
            for c in psutil.net_connections(kind="tcp"):
                if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                    pids.add(c.pid)
        except Exception:
            pass
    return sorted(pids)


def gpu_users() -> list[tuple[int, str, int]]:
    """(pid, name, MiB) for processes using the NVIDIA GPU, from nvidia-smi ([] when unknown)."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    users = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 3 and parts[0].isdigit():
            try:
                users.append((int(parts[0]), parts[1], int(float(parts[2]))))
            except ValueError:
                continue
    return users
