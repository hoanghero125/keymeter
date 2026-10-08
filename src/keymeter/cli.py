"""Command line: `keymeter web`, `keymeter tui` and `keymeter json`.

Settings come from the environment (empty variables count as unset), then from a .env file: --env FILE is always
read, the default ./.env only when KEYMETER_KEY is not already set in the environment.
  KEYMETER_KEY      the API key to watch (required)
  KEYMETER_URL      gateway base URL (default depends on the gateway)
  KEYMETER_GATEWAY  auto, litellm or openrouter (default auto)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from keymeter import __version__, gateways
from keymeter.monitor import Monitor
from keymeter.util import tidy


def env(name):
    """An environment variable without surrounding blanks and quotes ("" when unset; cmd's `set NAME=""` keeps the quotes)."""
    return os.environ.get(name, "").strip().strip('"')


def load_env(path, required):
    """Minimal .env reader (KEY=VALUE lines); real environment variables win unless they are empty."""
    if not path.is_file():
        if required:
            sys.exit(f"--env file not found: {path}")
        return
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        sys.exit(f"can't read {path}: save it as UTF-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if not env(k.strip()):  # `export KEYMETER_KEY=` must not hide the file's key
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


def build_parser():
    conn = argparse.ArgumentParser(add_help=False)
    g = conn.add_argument_group("gateway")
    g.add_argument("--gateway", choices=["auto", *gateways.GATEWAYS],
                   help="gateway type (default: $KEYMETER_GATEWAY or auto: openrouter when the URL's host is openrouter.ai "
                        "or a subdomain of it, or, if no URL is set, when the key starts with sk-or-; litellm otherwise)")
    g.add_argument("--url", help="gateway base URL (default: $KEYMETER_URL, else http://localhost:4000 for "
                                 "litellm or https://openrouter.ai/api for openrouter)")
    g.add_argument("--env", metavar="FILE", help="read settings from this file (default: ./.env, read only when KEYMETER_KEY "
                                                 "is not already set in the environment)")

    watch = argparse.ArgumentParser(add_help=False)
    w = watch.add_argument_group("monitoring")
    w.add_argument("-i", "--interval", type=float, default=15, help="seconds between polls (default 15)")
    w.add_argument("--window", type=float, default=15, help="burn-rate window in minutes (default 15)")
    w.add_argument("--warn", type=float, default=0.8, help="warn at this fraction of a budget (default 0.8)")
    w.add_argument("--crit", type=float, default=0.95, help="critical at this fraction (default 0.95)")
    w.add_argument("--log", metavar="CSV", help="also append every poll to this CSV file")

    p = argparse.ArgumentParser(
        prog="keymeter",
        description="Live spend, budget and burn rate for one LiteLLM or OpenRouter API key.",
        epilog="The key is read from KEYMETER_KEY (environment or .env). Run `keymeter COMMAND -h` for a command's options.")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    web = sub.add_parser("web", parents=[conn, watch], help="browser dashboard",
                         description="Serve a live dashboard for the key in the browser.")
    web.add_argument("--host", default="127.0.0.1",
                     help="address to listen on (default 127.0.0.1, this machine only; 0.0.0.0 = all interfaces, no login)")
    web.add_argument("--port", type=int, default=8765, help="port (default 8765)")

    tui = sub.add_parser("tui", parents=[conn, watch], help="terminal dashboard",
                         description="Show a live dashboard for the key in the terminal.")
    tui.add_argument("--once", action="store_true", help="print one snapshot and exit (exit 1 if not OK)")
    tui.add_argument("--no-team", action="store_true", help="don't query the team (LiteLLM /team/info)")
    tui.add_argument("--no-bell", action="store_true", help="don't beep on alerts")

    js = sub.add_parser("json", parents=[conn], help="print one snapshot as JSON and exit",
                        description="Poll once and print the summary as JSON. Exits 1 if the status is not OK.")
    js.add_argument("--no-team", action="store_true", help="don't query the team (LiteLLM /team/info)")
    js.set_defaults(interval=15, window=15, warn=0.8, crit=0.95, log=None)
    return p


def main(argv=None):
    # write UTF-8 even when output goes to a pipe or file on Windows (Python's default from 3.15 on, PEP 686)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    args.interval = max(args.interval, 1)
    if args.env:
        load_env(Path(args.env), required=True)
    elif not env("KEYMETER_KEY"):
        # ./.env is only a fallback for the key itself: a .env in whatever directory you are in (a cloned
        # repo, say) must not point a key from your environment at another server
        load_env(Path(".env"), required=False)
    key = env("KEYMETER_KEY")
    if not key:
        sys.exit("KEYMETER_KEY is not set. Put it in a .env file in this directory, pass --env FILE, or export it.")
    name = (args.gateway or env("KEYMETER_GATEWAY") or "auto").lower()
    team = args.command != "web" and not args.no_team  # the web dashboard covers the key only
    try:
        gw = gateways.create(name, args.url or env("KEYMETER_URL"), key, team=team)
    except ValueError as e:
        sys.exit(str(e))
    mon = Monitor(gw, args)

    if args.command == "json":
        mon.poll()
        print(json.dumps(tidy(mon.summary()), indent=2, ensure_ascii=False))
        return 0 if mon.snap.status == "OK" else 1
    if args.command == "web":
        from keymeter import web
        return web.serve(mon, args)
    from keymeter import tui
    return tui.run(mon, args)
