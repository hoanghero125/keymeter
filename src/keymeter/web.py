"""Browser dashboard (`keymeter web`).

Runs the Monitor of the watched key in a background thread and serves the dashboard on top of it. The page can
pick another saved key or add one; keys go from the page to this process, never back: the page only sees them
masked.

Endpoints:
  GET  /                    the dashboard
  GET  /api/state?since=TS  latest summary + spend history newer than TS, and the keys to pick from
  POST /api/poll            poll the gateway now
  POST /api/keys/select     {"name"}: watch another key
  POST /api/keys/add        {"gateway", "key", "url", "name", "force"}: check a key, save it and watch it
  POST /api/keys/remove     {"name"}: forget a saved key
The /api/keys endpoints take JSON only (a form on another site can't send it) and answer only on loopback.
"""
import json
import math
import socket
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from keymeter import config
from keymeter.monitor import Monitor
from keymeter.util import mask, num, tidy

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
HISTORY_S = 24 * 3600   # spend history kept in memory for the charts
MANUAL_GAP = 2          # min seconds between "poll now" requests
MAX_BODY = 64 * 1024    # bytes a POST may send
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class Poller(threading.Thread):
    """Owns the Monitor: polls on its schedule and publishes a snapshot for the HTTP threads."""

    def __init__(self, mon):
        super().__init__(daemon=True)
        self.mon = mon
        self.boot = uuid.uuid4().hex[:8]  # lets the page notice a restart or another key, and drop its history
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.history = deque(maxlen=int(HISTORY_S / mon.args.interval) + 1)  # [ts, key_spend]
        self.summary, self.summary_ts = tidy(mon.summary()), time.time()
        self.events, self.stale = [], False
        self.version = self.bell_seq = 0
        self.last_manual = 0.0

    def run(self):
        mon = self.mon
        while not self.stopped.is_set():
            self.wake.clear()  # before polling, so a "poll now" that arrives meanwhile is kept
            mon.polling = True
            snap = mon.poll()
            mon.polling = False
            mon.next_poll_at = time.time() + mon.next_delay()
            with self.lock:
                spend = num(snap.key.get("spend")) or 0.0
                if snap.key and math.isfinite(spend):  # a NaN or inf spend has no place on the chart, nor in JSON
                    self.history.append([round(snap.ts, 3), round(spend, 6)])
                if mon.bell:
                    self.bell_seq += 1
                    mon.bell = False
                self.summary, self.summary_ts = tidy(mon.summary()), time.time()
                self.events = [{"ts": round(ts, 3), "level": lvl, "text": txt} for ts, lvl, txt in mon.events]
                self.stale = mon.last_data is not None and mon.snap is not mon.last_data
                self.version += 1
            self.wake.wait(max(mon.next_poll_at - time.time(), 0))

    def stop(self):
        """Stop polling once the poll under way, if any, is done."""
        self.stopped.set()
        self.wake.set()

    def poll_now(self):
        now = time.time()
        if self.mon.polling or now - self.last_manual < MANUAL_GAP:
            return False
        self.last_manual = now
        self.wake.set()
        return True

    def state(self, since):
        mon, args = self.mon, self.mon.args
        with self.lock:
            out = {
                "boot": self.boot,
                "version": self.version,
                "summary_time": self.summary_ts,
                "summary": self.summary,
                "events": self.events,
                "stale": self.stale,
                "bell_seq": self.bell_seq,
                "history": [h for h in self.history if h[0] > since],
            }
        snap = mon.snap
        out["poller"] = {
            "polling": mon.polling,
            "polls": mon.polls,
            "fails": mon.fails,
            "fail_streak": mon.fail_streak,
            "last_poll_at": snap.ts if snap else None,
            "latency_ms": round(snap.latency_ms) if snap else None,
            "next_poll_at": mon.next_poll_at,
        }
        out["config"] = {"interval": args.interval, "window": args.window, "warn": args.warn,
                         "crit": args.crit, "history_s": HISTORY_S}
        out["server_time"] = time.time()
        return out


def no_key_state(args):
    """/api/state before there is a key to watch."""
    now = time.time()
    return {"boot": "", "version": 0, "summary_time": now, "summary": None, "events": [], "stale": False, "bell_seq": 0,
            "history": [], "poller": None, "server_time": now,
            "config": {"interval": args.interval, "window": args.window, "warn": args.warn, "crit": args.crit,
                       "history_s": HISTORY_S}}


class Dashboard:
    """The keys the page picks from (the saved ones, plus the key from the environment, which is never saved) and
    the Poller of the one it watches, or None before there is a key."""

    def __init__(self, args, extra=None):
        self.args, self.extra = args, extra
        self.lock = threading.Lock()
        self.poller = None
        self.watched = None    # the name of the watched key
        self.manage = True     # whether the page may change the keys; make_server() turns it off beyond loopback

    def start(self, name=None):
        """Watch the saved key `name`, else the key from the environment, else the saved key watched last."""
        store = config.load()
        e = config.find(store, name) if name else self.extra or config.find(store, store["active"])
        if name and e is None:
            raise config.Error(f"no saved key named {name!r}; `keymeter keys` lists them")
        e = e or (store["keys"][0] if store["keys"] else None)
        if e:
            self._watch(e)

    def _watch(self, e):
        poller = Poller(Monitor(config.adapter(e, team=False), self.args))  # the page covers the key only
        with self.lock:
            old, self.poller, self.watched = self.poller, poller, e["name"]
        poller.start()
        if old:
            old.stop()

    def _forget(self):
        with self.lock:
            old, self.poller, self.watched = self.poller, None, None
        if old:
            old.stop()

    def select(self, name):
        if self.extra and name == self.extra["name"]:
            return self._watch(self.extra)
        e = config.find(config.load(), name)
        if e is None:
            raise config.Error(f"no saved key named {name!r}")
        config.select(name)
        self._watch(e)

    def add(self, gateway, key, url, name, force):
        """Check the key, save it and watch it -> "" or, when the check fails and `force` is off, what the gateway
        answered (nothing saved)."""
        e = config.new_entry(config.load(), gateway, key, url, name)
        problem = "" if force else config.check(e)
        if not problem:
            config.add(e)
            self._watch(e)
        return problem

    def remove(self, name):
        config.remove(name)
        if name == self.watched:
            store = config.load()
            e = self.extra or config.find(store, store["active"])
            if e:
                self._watch(e)
            else:
                self._forget()

    def poll_now(self):
        poller = self.poller
        return poller.poll_now() if poller else False

    def state(self, since):
        poller = self.poller
        out = poller.state(since) if poller else no_key_state(self.args)
        try:
            saved, out["keys_error"] = config.load()["keys"], None
        except config.Error as e:
            saved, out["keys_error"] = [], str(e)
        rows = ([(self.extra, False)] if self.extra else []) + [(e, True) for e in saved]
        out["keys"] = [{**config.public(e), "saved": is_saved} for e, is_saved in rows]
        out["watched"] = self.watched if poller else None
        out["manage"] = self.manage
        return out


def text(body, name):
    v = body.get(name)
    return v if isinstance(v, str) else ""


def select_key(dash, body):
    dash.select(text(body, "name"))
    return {"ok": True}


def add_key(dash, body):
    problem = dash.add(text(body, "gateway"), text(body, "key"), text(body, "url"), text(body, "name"), body.get("force") is True)
    return {"ok": not problem, "problem": problem}


def remove_key(dash, body):
    dash.remove(text(body, "name"))
    return {"ok": True}


KEY_ACTIONS = {"/api/keys/select": select_key, "/api/keys/add": add_key, "/api/keys/remove": remove_key}


class Handler(BaseHTTPRequestHandler):
    dash = None             # set on the subclass make_server() creates
    loopback_only = False   # True when listening on a loopback address

    def do_GET(self):
        url = self._target()
        if url is None:
            return
        if url.path == "/api/state":
            try:
                since = float(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            return self._json(200, self.dash.state(since))
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = self._target()
        if url is None:
            return
        if url.path == "/api/poll":
            return self._json(202, {"queued": self.dash.poll_now()})
        action = KEY_ACTIONS.get(url.path)
        if action is None:
            return self._json(404, {"error": "not found"})
        if not self.dash.manage:
            return self._json(403, {"error": "keys can be changed here only while keymeter listens on this machine alone; "
                                             "use `keymeter add`, `keymeter use` and `keymeter remove` in a terminal"})
        body = self._body()
        if body is None:
            return
        try:
            self._json(200, action(self.dash, body))
        except config.Error as e:
            self._json(400, {"error": str(e)})

    def _body(self):
        """The JSON object a POST sent, or None once an error has been sent. Only JSON is taken, and only from this
        machine's own pages: a form on another site can't send JSON, and a script there would need CORS, which
        keymeter doesn't answer."""
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in LOOPBACK:
            self._json(403, {"error": "requests from other sites are refused"})
            return None
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            self._json(415, {"error": "send JSON"})
            return None
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if not 0 <= size <= MAX_BODY:
            self._json(413, {"error": f"send at most {MAX_BODY} bytes"})
            return None
        try:
            body = json.loads(self.rfile.read(size) or b"null")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._json(400, {"error": "send a JSON object"})
            return None
        return body

    def _target(self):
        """The parsed request target, or None once an error has been sent. On a loopback address, answer only
        requests addressed to localhost. This stops a web page from reading the dashboard through DNS rebinding
        (pointing its own domain at 127.0.0.1)."""
        try:
            url, host = urlparse(self.path), urlparse("//" + (self.headers.get("Host") or "")).hostname
        except ValueError:  # a malformed request target or Host header, such as "http://[/" or "["
            self._json(400, {"error": "malformed request target or Host header"})
            return None
        if not self.loopback_only or host is None or host in LOOPBACK:
            return url
        self._json(403, {"error": "keymeter answers only requests addressed to localhost; "
                                  "run it with --host 0.0.0.0 to serve other host names"})
        return None

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # the page polls every 2s; keep the console quiet
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != "win32"  # on Windows SO_REUSEADDR lets a second server share the port


class Server6(Server):
    address_family = socket.AF_INET6


def make_server(dash, host, port):
    """HTTP server for the dashboard; beyond loopback, with no login, the page can't change the keys."""
    dash.manage = host in LOOPBACK
    handler = type("BoundHandler", (Handler,), {"dash": dash, "loopback_only": host in LOOPBACK})
    return (Server6 if ":" in host else Server)((host, port), handler)


def own_ip():
    """This machine's address on its outbound interface (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def serve(args, extra=None):
    dash = Dashboard(args, extra)
    try:
        server = make_server(dash, args.host, args.port)
    except OSError as e:
        sys.exit(f"can't listen on {args.host}:{args.port}: {e}")
    dash.start(args.name)
    port = server.server_address[1]
    local = args.host in LOOPBACK
    shown = "localhost" if local else own_ip() if args.host in ("0.0.0.0", "", "::") else args.host
    if ":" in shown:
        shown = f"[{shown}]"
    if dash.poller:
        gw = dash.poller.mon.gateway
        print(f"keymeter on http://{shown}:{port}  ({gw.name} {gw.url}, key {mask(gw.key)}, poll every {args.interval:g}s)", flush=True)
    elif local:
        print(f"keymeter on http://{shown}:{port}  (no key yet: open the page and add one)", flush=True)
    else:
        print(f"keymeter on http://{shown}:{port}  (no key yet: add one with `keymeter add`)", flush=True)
    if not local:
        print(f"Warning: listening on {args.host or 'all interfaces'} with no login. Anyone who can reach port {port} "
              "can see the watched key's spend and budget. To reach a remote server, an SSH tunnel is safer (see the README).",
              flush=True)
    print("Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if dash.poller:
        mon = dash.poller.mon
        print(f"Stopped after {mon.polls} polls ({mon.fails} failed).")
    return 0
