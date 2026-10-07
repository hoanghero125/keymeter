import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from keymeter.gateways import Snapshot

KEY = "sk-test-abcdefghijkl1234"


class FakeGateway:
    """A local HTTP server answering like a gateway: routes map a path to (status, json body).

    A route value can also be a function of the query dict, for answers that depend on the request.
    Every request is recorded as (path, query, authorization header).
    """

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append((url.path, query, self.headers.get("Authorization")))
                route = fake.routes.get(url.path, (404, {"detail": "Not Found"}))
                status, body = route(query) if callable(route) else route
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_gateway():
    started = []

    def start(routes):
        gw = FakeGateway(routes)
        started.append(gw)
        return gw

    yield start
    for gw in started:
        gw.close()


class Scripted:
    """A gateway adapter that hands out prepared snapshots, for testing the monitor without HTTP."""

    name = "litellm"
    url = "http://gateway.test"
    key = KEY
    features = frozenset({"models", "limits", "team"})

    def __init__(self, *snaps):
        self.snaps = list(snaps)

    def fetch(self):
        return self.snaps.pop(0)


def ok(ts, **key):
    return Snapshot(ts, "OK", key=key)


@pytest.fixture
def args():
    return SimpleNamespace(interval=15, window=15, warn=0.8, crit=0.95, log=None)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No KEYMETER_* variables and an empty working directory (so the developer's own .env is not read)."""
    for name in ("KEYMETER_KEY", "KEYMETER_URL", "KEYMETER_GATEWAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path
