import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import KEY

from keymeter.gateways import LiteLLM
from keymeter.monitor import Monitor
from keymeter.web import make_server


@pytest.fixture
def dashboard(fake_gateway, args):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 2.5, "max_budget": 10, "model_spend": {"gpt-x": 2.5}}})})
    server, poller = make_server(Monitor(LiteLLM(gw.url, KEY), args), "127.0.0.1", 0)
    poller.start()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    deadline = time.time() + 5
    while get(base, "/api/state")[2]["version"] < 1:  # wait for the first gateway poll
        assert time.time() < deadline, "first poll never finished"
        time.sleep(0.02)
    yield base
    server.shutdown()
    server.server_close()


def get(base, path, method="GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, method=method)) as r:
            body = r.read()
            status, ctype = r.status, r.headers["Content-Type"]
    except urllib.error.HTTPError as e:
        body, status, ctype = e.read(), e.code, e.headers["Content-Type"]
    return status, ctype, json.loads(body) if ctype.startswith("application/json") else body.decode()


def test_serves_the_page_and_assets(dashboard):
    status, ctype, page = get(dashboard, "/")
    assert status == 200 and ctype.startswith("text/html") and "<title>keymeter</title>" in page
    assert get(dashboard, "/app.js")[1].startswith("text/javascript")
    assert get(dashboard, "/styles.css")[1].startswith("text/css")


def test_state(dashboard):
    status, _, d = get(dashboard, "/api/state")
    assert status == 200
    s = d["summary"]
    assert s["status"] == "OK" and s["key_info"]["spend"] == 2.5
    assert s["features"] == ["limits", "models", "team"]
    assert len(d["history"]) == 1 and d["history"][0][1] == 2.5
    assert d["poller"]["polls"] == 1 and d["config"]["interval"] == 15
    assert get(dashboard, f"/api/state?since={d['history'][0][0]}")[2]["history"] == []


def test_state_never_contains_the_key(dashboard):
    assert KEY not in json.dumps(get(dashboard, "/api/state")[2])


def test_poll_now(dashboard):
    status, _, d = get(dashboard, "/api/poll", method="POST")
    assert status == 202 and d == {"queued": True}
    assert get(dashboard, "/api/poll", method="POST")[2] == {"queued": False}  # rate limited


def test_unknown_paths(dashboard):
    assert get(dashboard, "/../pyproject.toml")[0] == 404
    assert get(dashboard, "/api/nope", method="POST")[0] == 404


def request(base, path, host):
    req = urllib.request.Request(base + path, headers={"Host": host})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_answers_only_localhost_names_on_loopback(dashboard):
    port = dashboard.rsplit(":", 1)[1]
    assert request(dashboard, "/api/state", f"localhost:{port}") == 200
    assert request(dashboard, "/api/state", f"[::1]:{port}") == 200
    assert request(dashboard, "/api/state", f"evil.example:{port}") == 403  # DNS rebinding
    assert request(dashboard, "/", "evil.example") == 403


def test_listens_on_ipv6_loopback(args):
    try:
        server, _ = make_server(Monitor(LiteLLM("http://127.0.0.1:9", KEY), args), "::1", 0)
    except OSError:
        pytest.skip("no IPv6 loopback here")
    assert server.server_address[0] == "::1"
    server.server_close()
