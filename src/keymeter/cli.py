"""Command line: `keymeter web`, `tui` and `json` watch a key; `add`, `keys`, `use` and `remove` manage the saved ones.

The key to watch is, in order:
  - the saved key NAME names (`keymeter tui work`)
  - KEYMETER_KEY from the environment (empty variables count as unset) or a .env file: --env FILE is always read,
    ./.env only when KEYMETER_KEY is not already set in the environment and the file sets it
      KEYMETER_KEY      the API key to watch
      KEYMETER_URL      gateway base URL (default depends on the gateway)
      KEYMETER_GATEWAY  auto, litellm, openrouter or deepseek (default auto)
  - the saved key watched last (`keymeter add`, `keymeter use`, or the web dashboard's Keys dialog)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from keymeter import __version__, config, gateways, prompts
from keymeter.monitor import Monitor
from keymeter.util import tidy

ENV_NAME = "KEYMETER_KEY"  # the name the key from the environment goes by among the saved ones
ENV_VARS = ("KEYMETER_KEY", "KEYMETER_URL", "KEYMETER_GATEWAY")
NO_KEY = ("No key to watch yet. Add one with `keymeter add`, open `keymeter web` and add it there, "
          "or set KEYMETER_KEY (in the environment or a .env file).")


def env(name):
    """An environment variable without surrounding blanks and quotes ("" when unset; cmd's `set NAME=""` keeps the quotes)."""
    return os.environ.get(name, "").strip().strip('"')


def env_lines(path):
    """(name, value) for each NAME=value line of a .env file."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        sys.exit(f"can't read {path}: save it as UTF-8")
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        lines.append((k.strip(), v.strip().strip('"').strip("'")))
    return lines


def load_env(path, required, names=None):
    """Minimal .env reader (KEY=VALUE lines), only the variables in `names` if given; real environment variables win
    unless they are empty."""
    if not path.is_file():
        if required:
            sys.exit(f"--env file not found: {path}")
        return
    for k, v in env_lines(path):
        if not env(k) and (names is None or k in names):  # `export KEYMETER_KEY=` must not hide the file's key
            os.environ[k] = v


def env_key(args):
    """The key from the environment or a .env file, as an entry that is watched but never saved; None without one."""
    if args.env:
        load_env(Path(args.env), required=True)
    elif not env("KEYMETER_KEY"):
        # ./.env is only a fallback for the key itself: a .env in whatever directory you are in (a cloned repo, say)
        # must not point a key from your environment, or a saved one, at another server. One without a key belongs
        # to some other program, so it is not read at all, and of one with a key only the KEYMETER_ settings count:
        # a proxy set there would carry the saved keys too, once one is picked (NAME, or the web page's Keys dialog).
        local = Path(".env")
        if local.is_file() and any(k == "KEYMETER_KEY" and v for k, v in env_lines(local)):
            load_env(local, required=False, names=ENV_VARS)
    key = env("KEYMETER_KEY")
    if not key:
        return None
    return config.entry((args.gateway or env("KEYMETER_GATEWAY") or "auto").lower(), key,
                        args.url or env("KEYMETER_URL"), ENV_NAME)


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def add_target_args(g, auto=True):
    g.add_argument("--gateway", choices=["auto", *gateways.GATEWAYS] if auto else list(gateways.GATEWAYS),
                   help="gateway type (default: $KEYMETER_GATEWAY or auto: openrouter when the URL's host is openrouter.ai "
                        "or a subdomain of it, or, if no URL is set, when the key starts with sk-or-; deepseek when the URL's "
                        "host is deepseek.com or a subdomain of it; litellm otherwise)" if auto else
                        "gateway type (asked when not given)")
    g.add_argument("--url", help="gateway base URL (default: $KEYMETER_URL, else http://localhost:4000 for "
                                 "litellm, https://openrouter.ai/api for openrouter or https://api.deepseek.com for deepseek)"
                   if auto else "gateway base URL (asked for litellm when not given)")


def build_parser():
    conn = argparse.ArgumentParser(add_help=False)
    conn.add_argument("name", nargs="?", metavar="NAME",
                      help="saved key to watch (default: KEYMETER_KEY, else the saved key watched last)")
    g = conn.add_argument_group("gateway (for KEYMETER_KEY, or instead of a saved key's own)")
    add_target_args(g)
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
        description="Live spend, budget and burn rate for one LiteLLM, OpenRouter or DeepSeek API key.",
        epilog="Start with `keymeter add` to save a key, then `keymeter web` or `keymeter tui`. "
               "Run `keymeter COMMAND -h` for a command's options.")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    web = sub.add_parser("web", parents=[conn, watch], help="browser dashboard, where you can also add and pick keys",
                         description="Serve a live dashboard for the key in the browser. Its Keys button adds and picks keys.")
    web.add_argument("--host", default="127.0.0.1",
                     help="address to listen on (default 127.0.0.1, this machine only; 0.0.0.0 = all interfaces, no login)")
    web.add_argument("--port", type=int, default=8765, help="port (default 8765)")

    tui = sub.add_parser("tui", parents=[conn, watch], help="terminal dashboard",
                         description="Show a live dashboard for the key in the terminal. Without NAME it first asks "
                                     "which saved key to watch; Ctrl+C goes back to that question.")
    tui.add_argument("--once", action="store_true", help="print one snapshot and exit (exit 1 if not OK)")
    tui.add_argument("--no-team", action="store_true", help="don't query the team (LiteLLM /team/info)")
    tui.add_argument("--no-bell", action="store_true", help="don't beep on alerts")

    js = sub.add_parser("json", parents=[conn], help="print one snapshot as JSON and exit",
                        description="Poll once and print the summary as JSON. Exits 1 if the status is not OK.")
    js.add_argument("--no-team", action="store_true", help="don't query the team (LiteLLM /team/info)")
    js.set_defaults(interval=15, window=15, warn=0.8, crit=0.95, log=None)

    add = sub.add_parser("add", help="save a key to watch", description="Ask for a key, check it with one poll and save "
                                                                        "it; keymeter watches it from then on.")
    add_target_args(add, auto=False)
    add.add_argument("--name", help="name for the key (asked when not given)")
    sub.add_parser("keys", help="list the saved keys", description="List the saved keys, masked.")
    use = sub.add_parser("use", help="watch another saved key from now on", description="Make NAME the key to watch.")
    use.add_argument("name", metavar="NAME")
    rm = sub.add_parser("remove", help="forget a saved key", description="Forget the saved key NAME.")
    rm.add_argument("name", metavar="NAME")
    return p


def cmd_add(args):
    if not prompts.add_key(args.gateway, args.url, args.name):
        return 1
    print("Watch it with `keymeter web` (browser) or `keymeter tui` (terminal).")
    return 0


def cmd_keys(args):
    store = config.load()
    if not store["keys"]:
        print("No saved keys. Add one with `keymeter add`.")
        return 0
    prompts.list_keys(store)
    print(f"\n* = the key `web`, `tui` and `json` watch. Saved in {config.path()}.")
    return 0


def cmd_use(args):
    config.select(args.name)
    print(f"keymeter watches {args.name!r} from now on.")
    return 0


def cmd_remove(args):
    config.remove(args.name)
    print(f"Removed {args.name!r} from keymeter (the key itself still works).")
    return 0


def chosen(args, extra):
    """The entry to watch: the saved key NAME names, the key from the environment, or the saved key watched last;
    --gateway and --url apply on top. None when there is none."""
    store = config.load()
    if args.name:
        e = config.find(store, args.name)
        if e is None:
            raise config.Error(f"no saved key named {args.name!r}; `keymeter keys` lists them")
    else:
        e = extra or config.find(store, store["active"]) or (store["keys"][0] if store["keys"] else None)
    if e is not extra and (args.gateway or args.url):
        e = config.entry(args.gateway or e["gateway"], e["key"], args.url or e.get("url"), e["name"])
    return e


def tui_menu(args):
    """Pick a key, watch it until Ctrl+C, pick again; q (or Ctrl+C at the question) quits."""
    from keymeter import tui
    args.switch = True
    while True:
        try:
            e = prompts.pick_key()
        except KeyboardInterrupt:
            print()
            return 0
        if e is None:
            return 0
        tui.run(Monitor(config.adapter(e, team=not args.no_team), args), args)
        print()


def watch(args):
    args.interval = max(args.interval, 1)
    extra = env_key(args)
    if args.command == "web":
        from keymeter import web
        return web.serve(args, extra)  # the page adds and picks keys itself
    if args.command == "tui" and not args.once and not args.name and not extra and interactive():
        return tui_menu(args)
    e = chosen(args, extra)
    if e is None:
        if not interactive():
            sys.exit(NO_KEY)
        e = prompts.add_key(args.gateway, args.url)
        if e is None:
            return 1
    mon = Monitor(config.adapter(e, team=not args.no_team), args)

    if args.command == "json":
        mon.poll()
        print(json.dumps(tidy(mon.summary()), indent=2, ensure_ascii=False))
        return 0 if mon.snap.status == "OK" else 1
    from keymeter import tui
    return tui.run(mon, args)


COMMANDS = {"add": cmd_add, "keys": cmd_keys, "use": cmd_use, "remove": cmd_remove}


def main(argv=None):
    # write UTF-8 even when output goes to a pipe or file on Windows (Python's default from 3.15 on, PEP 686)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS.get(args.command, watch)(args)
    except config.Error as e:
        sys.exit(str(e))
    except (KeyboardInterrupt, EOFError):  # Ctrl+C, or input closed, while keymeter asks a question
        sys.exit("\ncancelled")
