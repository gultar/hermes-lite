"""hermes-lite — a lightweight Hermes Agent for local llama.cpp models."""

from __future__ import annotations

import argparse
import os
import shlex
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hermes-lite", description="Lightweight Hermes Agent for local models.")
    p.add_argument("-C", "--cwd", default=os.getcwd(), help="working directory for the agent (default: here)")
    p.add_argument("--config", help="config file (default: ~/.hermes-lite/config.yaml)")
    p.add_argument("--no-server", action="store_true", help="don't launch llama-server; use one already running")
    p.add_argument("-r", "--resume", metavar="ID", help="resume a saved session (id or unique prefix)")
    p.add_argument("--yolo", action="store_true", help="never ask before dangerous commands")
    p.add_argument("--no-think", action="store_true", help="send enable_thinking=false")
    p.add_argument("--no-mouse", action="store_true",
                   help="let the terminal handle the mouse: drag selects and copies like any terminal "
                        "(open panels with Tab and Enter instead of clicks)")
    p.add_argument("--no-loop-guard", action="store_true",
                   help="this run without the thinking-loop guard (same as loop_guard.enabled: false)")
    p.add_argument("--default-prompts", action="store_true",
                   help="this run with the built-in prompts, ignoring your copies in ~/.hermes-lite/prompts")
    p.add_argument("--hermes-prompts", action="store_true",
                   help="this run with the original Hermes prompt wording (same as prompts.style: hermes)")
    p.add_argument("--telegram", action="store_true",
                   help="run with no TUI: talk to Hermes Lite from Telegram only (pairs your account first)")
    p.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE", default=[],
                   help="change one setting in config.yaml and exit (repeatable), e.g. --set server.port=8080")
    p.add_argument("--give-back", action="store_true",
                   help="start again the llama-servers Hermes Lite stopped to make room (server.stop_others) and exit")
    p.add_argument("--print-server-cmd", action="store_true", help="print the llama-server command and exit")
    p.add_argument("--rollback", nargs="?", const="code", choices=("code", "config", "latest", "list"),
                   help="put back the previous code version (default), the previous config.yaml (config), "
                        "the newest saved code (latest, undoes a rollback), or show what is saved (list)")
    p.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)  # used by install.sh
    args = p.parse_args(argv)

    from . import rollback
    if args.snapshot:
        rollback.snapshot_code()
        return 0
    cfg = load_config(args.config)
    from pathlib import Path
    cfg_path = Path(cfg["_path"])
    if args.rollback:
        if args.rollback == "list":
            return rollback.list_versions(cfg_path)
        if args.rollback == "config":
            return rollback.rollback_config(cfg_path)
        return rollback.rollback_code(latest=args.rollback == "latest")
    rollback.snapshot_config(cfg_path)
    if args.give_back:
        from .server import LlamaServer
        started = LlamaServer(cfg).give_back()
        print("Started again: " + ", ".join(started) if started else "Nothing to start: no server is waiting "
              "(or its port is in use again).")
        return 0
    if args.set:
        from .config import set_value
        for item in args.set:
            key, sep, value = item.partition("=")
            if not sep:
                print(f"--set {item}: expected section.key=value")
                return 2
            print(set_value(cfg_path, key.strip(), value.strip()), f"({cfg_path})")
        print("Undo with: hermes-lite --rollback config")
        return 0
    from . import promptfiles
    promptfiles.ENABLED = bool(cfg.get_path("prompts.overrides", True)) and not args.default_prompts
    promptfiles.set_style("hermes" if args.hermes_prompts else str(cfg.get_path("prompts.style", "compact")))
    promptfiles.snapshot_overrides()
    if args.yolo:
        cfg["agent"]["approvals"] = "off"
    if args.no_loop_guard:
        cfg.setdefault("loop_guard", {})["enabled"] = False
    if args.no_think:
        cfg["model"]["thinking"] = False
    if args.print_server_cmd:
        from .server import LlamaServer, build_command
        srv = LlamaServer(cfg)
        print(" ".join(shlex.quote(c) for c in build_command(cfg, srv.rung)))
        from .server import RAM_WARNINGS
        for w in RAM_WARNINGS:
            print("# " + w)
        if srv.rung:
            print(f"# fallback setting in use: {srv.rung_label()}  (/server reset in Hermes Lite to retry your own)")
        return 0

    if args.telegram and not _telegram_ready():
        return 1
    from .tui.app import HermesApp
    app = HermesApp(cfg, cwd=os.path.abspath(os.path.expanduser(args.cwd)),
                    start_server=not args.no_server and cfg.get_path("server.autostart", True),
                    resume=args.resume, telegram=args.telegram, headless=args.telegram)
    if args.telegram:
        print("Hermes Lite is running with no TUI: talk to it on Telegram. Ctrl+C quits.", flush=True)
        app.run(headless=True)
    else:
        app.run(mouse=not (args.no_mouse or cfg.get_path("ui.mouse", True) is False))
    return 0


def _telegram_ready() -> bool:
    """--telegram: make sure there is a bot token and a paired account, asking in this terminal."""
    import asyncio
    import getpass
    from . import telegram as tg
    from .config import home
    h = home()
    if not tg.token(h):
        print("Create a bot with @BotFather in Telegram (/newbot) and paste its token here (hidden).")
        value = getpass.getpass("Bot token: ").strip()
        if not tg.TOKEN_RE.match(value):
            print("That does not look like a bot token.")
            return False
        tg.save_state(h, {**tg.load_state(h), "token": value})
        print(f"Saved in {tg.state_path(h)} (readable only by you).")
    if not tg.load_state(h).get("owner_id"):
        try:
            return asyncio.run(tg.pair_in_terminal(h))
        except Exception as exc:
            print(f"Telegram: {exc}")
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
