"""Memory watchdog: keeps a runaway command from taking the whole desktop down.

Every few seconds it reads the free memory (/proc/meminfo on Linux, psutil elsewhere). When available RAM falls under the danger line, it finds
the largest process tree that Hermes Lite started (tool commands and background jobs, never
llama-server) and stops it, then reports what it did. It also writes a short memory log so a
problem that slips through can be traced afterwards.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import oscompat

GIB = 1024 ** 3


def meminfo() -> dict[str, int]:
    return oscompat.meminfo()


def proc_table() -> dict[int, tuple[int, int, int]]:
    """pid -> (ppid, pgid, rss_bytes) for every visible process."""
    return oscompat.process_table()


def tree(table: dict, root: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen, stack = set(), [root]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(children.get(p, []))
    return seen


def gib(n: int) -> str:
    return f"{n / GIB:.1f} GB"


class MemoryWatchdog:
    def __init__(self, log_path: Path, *, min_free_gib: float = 1.5, min_free_fraction: float = 0.06,
                 server_pid: Callable[[], Optional[int]] = lambda: None,
                 label: Callable[[int], str] = lambda pid: f"process {pid}",
                 on_kill: Callable[[int, str, int, int], None] = lambda pid, label, rss, avail: None,
                 on_warn: Callable[[str], None] = lambda text: None, interval: float = 2.0):
        self.log_path = log_path
        self.min_free_gib, self.min_free_fraction = min_free_gib, min_free_fraction
        self.server_pid, self.label, self.on_kill, self.on_warn = server_pid, label, on_kill, on_warn
        self.interval = interval
        self.stop_event = threading.Event()
        self._last_log = 0.0
        self._last_warn = 0.0

    def start(self) -> "MemoryWatchdog":
        threading.Thread(target=self._run, daemon=True, name="memwatch").start()
        return self

    def stop(self) -> None:
        self.stop_event.set()

    def threshold(self, total: int) -> int:
        return int(max(self.min_free_gib * GIB, total * self.min_free_fraction))

    def _children(self, table: dict) -> list[tuple[int, int, int]]:
        """(root pid, pgid, tree rss) for each direct child tree, server excluded."""
        me = os.getpid()
        srv = self.server_pid()
        out = []
        for pid, (ppid, pgid, _) in table.items():
            if ppid != me:
                continue
            members = tree(table, pid)
            if srv and srv in members:
                continue
            out.append((pid, pgid, sum(table[m][2] for m in members if m in table)))
        return sorted(out, key=lambda x: -x[2])

    def check_once(self) -> None:
        mi = meminfo()
        total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        if not total:
            return
        now = time.monotonic()
        need_log = now - self._last_log >= 30
        danger = avail < self.threshold(total)
        if not (need_log or danger):
            return
        table = proc_table()
        kids = self._children(table)
        srv = self.server_pid()
        srv_rss = sum(table[p][2] for p in tree(table, srv) if p in table) if srv and srv in table else 0
        own = table.get(os.getpid(), (0, 0, 0))[2]
        if need_log:
            self._last_log = now
            top = f"{self.label(kids[0][0])[:60]} {gib(kids[0][2])}" if kids else "none"
            self._log(f"available {gib(avail)} of {gib(total)} | hermes-lite {gib(own)} | "
                      f"llama-server {gib(srv_rss)} | biggest tool tree: {top}")
        if not danger:
            return
        if kids and kids[0][2] >= 200 * 1024 ** 2:
            pid, pgid, rss = kids[0]
            label = self.label(pid)
            self._log(f"LOW MEMORY ({gib(avail)} available): stopping '{label}' using {gib(rss)}")
            self.on_kill(pid, label, rss, avail)  # first, so the tool result can say why it stopped
            self._kill(pid, pgid)
            time.sleep(3)  # let the kernel reclaim before judging again
        elif now - self._last_warn > 120:
            self._last_warn = now
            self._log(f"LOW MEMORY ({gib(avail)} available), nothing of Hermes Lite's to stop "
                      f"(llama-server {gib(srv_rss)}, hermes-lite {gib(own)})")
            self.on_warn(f"The computer is almost out of memory ({gib(avail)} available). llama-server "
                         f"uses {gib(srv_rss)}; nothing Hermes Lite started is large. Close other apps, "
                         "or see /server for lighter server settings.")

    def _kill(self, pid: int, pgid: int) -> None:
        if not hasattr(os, "killpg"):  # Windows: the whole tree, with taskkill
            oscompat.kill_tree(pid, grace=1.0)
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if pgid and pgid != os.getpgid(0):
                    os.killpg(pgid, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                return
            time.sleep(1.0)

    def _log(self, line: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
            if self.log_path.stat().st_size > 2_000_000:  # keep the log small
                text = self.log_path.read_text(encoding="utf-8").splitlines()[-5000:]
                self.log_path.write_text("\n".join(text) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.check_once()
            except Exception as exc:  # the watchdog must never take Hermes Lite down
                self._log(f"watchdog error: {exc!r}")
