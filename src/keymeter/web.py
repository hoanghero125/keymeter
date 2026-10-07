"""Browser dashboard (`keymeter web`).

Runs the Monitor in a background thread and serves the dashboard on top of it. The key never leaves
this process; the page only sees the masked key.

Endpoints:
  GET  /                    the dashboard
  GET  /api/state?since=TS  latest summary + spend history newer than TS
  POST /api/poll            poll the gateway now
"""
import json
import socket
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from keymeter.util import mask, num, tidy

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
HISTORY_S = 24 * 3600   # spend history kept in memory for the charts
MANUAL_GAP = 2          # min seconds between "poll now" requests
BOOT_ID = uuid.uuid4().hex[:8]  # lets the page notice a server restart and drop its history
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class Poller(threading.Thread):
    """Owns the Monitor: polls on its schedule and publishes a snapshot for the HTTP threads."""

    def __init__(self, mon):
        super().__init__(daemon=True)
        self.mon = mon
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.history = deque(maxlen=int(HISTORY_S / mon.args.interval) + 1)  # [ts, key_spend]
        self.summary, self.summary_ts = tidy(mon.summary()), time.time()
        self.events, self.stale = [], False
        self.version = self.bell_seq = 0
        self.last_manual = 0.0

    def run(self):
        mon = self.mon
        while True:
            self.wake.clear()  # before polling, so a "poll now" that arrives meanwhile is kept
            mon.polling = True
            snap = mon.poll()
            mon.polling = False
            mon.next_poll_at = time.time() + mon.next_delay()
            with self.lock:
                if snap.key:
                    self.history.append([round(snap.ts, 3), round(num(snap.key.get("spend")) or 0.0, 6)])
                if mon.bell:
                    self.bell_seq += 1
                    mon.bell = False
                self.summary, self.summary_ts = tidy(mon.summary()), time.time()
                self.events = [{"ts": round(ts, 3), "level": lvl, "text": txt} for ts, lvl, txt in mon.events]
                self.stale = mon.last_data is not None and mon.snap is not mon.last_data
                self.version += 1
            self.wake.wait(max(mon.next_poll_at - time.time(), 0))

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
                "boot": BOOT_ID,
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


class Handler(BaseHTTPRequestHandler):
    poller = None           # set on the subclass make_server() creates
    loopback_only = False   # True when listening on a loopback address

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        if url.path == "/api/state":
            try:
                since = float(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            return self._json(200, self.poller.state(since))
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        if urlparse(self.path).path == "/api/poll":
            return self._json(202, {"queued": self.poller.poll_now()})
        self._json(404, {"error": "not found"})

    def _host_ok(self):
        """On a loopback address, answer only requests addressed to localhost. This stops a web page from
        reading the dashboard through DNS rebinding (pointing its own domain at 127.0.0.1)."""
        host = urlparse("//" + (self.headers.get("Host") or "")).hostname
        if not self.loopback_only or host is None or host in LOOPBACK:
            return True
        self._json(403, {"error": "keymeter answers only requests addressed to localhost; "
                                  "run it with --host 0.0.0.0 to serve other host names"})
        return False

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


def make_server(mon, host, port):
    """HTTP server plus the (not yet started) poller thread that feeds it."""
    poller = Poller(mon)
    handler = type("BoundHandler", (Handler,), {"poller": poller, "loopback_only": host in LOOPBACK})
    return (Server6 if ":" in host else Server)((host, port), handler), poller


def own_ip():
    """This machine's address on its outbound interface (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def serve(mon, args):
    gw = mon.gateway
    try:
        server, poller = make_server(mon, args.host, args.port)
    except OSError as e:
        sys.exit(f"can't listen on {args.host}:{args.port}: {e}")
    poller.start()
    port = server.server_address[1]
    local = args.host in LOOPBACK
    shown = "localhost" if local else own_ip() if args.host in ("0.0.0.0", "", "::") else args.host
    if ":" in shown:
        shown = f"[{shown}]"
    print(f"keymeter on http://{shown}:{port}  ({gw.name} {gw.url}, key {mask(gw.key)}, poll every {args.interval:g}s)", flush=True)
    if not local:
        print(f"Warning: listening on {args.host or 'all interfaces'} with no login. Anyone who can reach port {port} "
              "can see this key's spend and budget. To reach a remote server, an SSH tunnel is safer (see the README).", flush=True)
    print("Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(f"Stopped after {mon.polls} polls ({mon.fails} failed).")
    return 0
