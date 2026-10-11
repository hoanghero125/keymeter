import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import KEY, Scripted, ok

from keymeter import config
from keymeter.monitor import Monitor
from keymeter.web import Dashboard, Poller, make_server

T0 = 1_800_000_000.0


def env_key(url):
    return config.entry("litellm", KEY, url, "KEYMETER_KEY")


def start(dash, host="127.0.0.1"):
    """Serve `dash` on a free port -> (base URL, server)."""
    server = make_server(dash, host, 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


def wait_for(base, check, what):
    deadline = time.time() + 5
    while not check(get(base, "/api/state")[2]):
        assert time.time() < deadline, what
        time.sleep(0.02)


@pytest.fixture
def dashboard(clean_env, fake_gateway, args):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 2.5, "max_budget": 10, "model_spend": {"gpt-x": 2.5}}})})
    dash = Dashboard(args, env_key(gw.url))
    dash.start()
    base, server = start(dash)
    wait_for(base, lambda d: d["version"] >= 1, "first poll never finished")
    yield base
    server.shutdown()
    server.server_close()
    dash.poller.stop()


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


@pytest.mark.parametrize("spends", [(0.0, 1e-300), ("nan", "nan")], ids=["tiny spend step", "spend not a number"])
def test_numbers_that_are_not_finite_keep_the_poller_alive(args, spends):
    # the second poll measures a burn rate that runs out in inf (or NaN) seconds: the poller must keep polling,
    # and the page's JSON.parse takes no NaN or Infinity
    poller = Poller(Monitor(Scripted(*(ok(T0 + 600 * i, spend=s, max_budget=1e9) for i, s in enumerate(spends))), args))
    poller.start()
    deadline = time.time() + 5
    while poller.version < 2:
        assert poller.is_alive() and time.time() < deadline, "the poller stopped"
        if poller.version == 1:
            poller.poll_now()
        time.sleep(0.02)
    d = json.loads(json.dumps(poller.state(0), allow_nan=False))
    assert d["summary"]["status"] == "OK" and d["summary"]["key_info"]["runs_out_in_s"] is None


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


def test_malformed_host_header_is_a_bad_request(dashboard):
    req = urllib.request.Request(dashboard + "/api/state", headers={"Host": "["})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400 and json.loads(e.value.read()) == {"error": "malformed request target or Host header"}


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_malformed_request_target_is_a_bad_request(dashboard, method):
    # an absolute-form target urlparse can't read must get an answer, not a dropped connection and a traceback
    with socket.create_connection(("127.0.0.1", int(dashboard.rsplit(":", 1)[1])), timeout=5) as s:
        s.sendall(f"{method} http://[/api/state HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n".encode())
        assert s.makefile("rb").readline().split()[1:2] == [b"400"]


def test_listens_on_ipv6_loopback(args):
    try:
        server = make_server(Dashboard(args), "::1", 0)
    except OSError:
        pytest.skip("no IPv6 loopback here")
    assert server.server_address[0] == "::1"
    server.server_close()


# ---------------------------------------------------------------- keys

def post(base, path, body, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def balance(total):
    return {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": total}]}


@pytest.fixture
def no_key(clean_env, args):
    dash = Dashboard(args)
    dash.start()
    base, server = start(dash)
    yield base, dash
    server.shutdown()
    server.server_close()
    if dash.poller:
        dash.poller.stop()


def test_without_a_key_the_page_gets_no_summary(no_key):
    base, _ = no_key
    d = get(base, "/api/state")[2]
    assert (d["summary"], d["keys"], d["watched"], d["manage"]) == (None, [], None, True)
    assert get(base, "/api/poll", method="POST")[2] == {"queued": False}


def test_add_checks_saves_and_watches_the_key(no_key, fake_gateway):
    base, _ = no_key
    gw = fake_gateway({"/user/balance": (200, balance("8.42"))})
    assert post(base, "/api/keys/add", {"gateway": "deepseek", "key": KEY, "url": gw.url}) == (200, {"ok": True, "problem": ""})
    assert config.load()["keys"] == [{"name": "deepseek", "gateway": "deepseek", "key": KEY, "url": gw.url}]
    wait_for(base, lambda d: d["summary"] and d["summary"]["status"] == "OK", "the new key is never watched")
    d = get(base, "/api/state")[2]
    assert d["watched"] == "deepseek" and d["summary"]["key_info"]["balance"] == 8.42
    assert d["keys"] == [{"name": "deepseek", "gateway": "deepseek", "key": "sk-tes…1234", "url": gw.url, "saved": True}]
    assert KEY not in json.dumps(d)


def test_add_reports_a_failed_check_and_saves_only_when_forced(no_key, fake_gateway):
    base, _ = no_key
    gw = fake_gateway({"/user/balance": (401, {"error": {"message": "Authentication Fails"}})})
    key = {"gateway": "deepseek", "key": KEY, "url": gw.url, "name": "ds"}
    assert post(base, "/api/keys/add", key) == (200, {"ok": False, "problem": "AUTH ERROR: Authentication Fails"})
    assert config.load()["keys"] == []
    assert post(base, "/api/keys/add", {**key, "force": True})[1]["ok"]
    assert config.load()["active"] == "ds"


@pytest.mark.parametrize(("body", "error"), [
    ({"gateway": "deepseek", "key": " "}, "the API key is empty"),
    ({"gateway": "openai", "key": KEY}, "unknown gateway 'openai'"),
    ({"gateway": "litellm", "key": KEY, "url": "llm.example.com"}, "must start with http:// or https://"),
    ({"gateway": "deepseek", "key": KEY, "name": "-x"}, "a key name is 1 to 40"),
    ({"gateway": 3, "key": [KEY]}, "the API key is empty"),
])
def test_add_refuses_what_it_can_tell_is_wrong(no_key, body, error):
    status, d = post(no_key[0], "/api/keys/add", body)
    assert status == 400 and error in d["error"]


def test_select_and_remove(no_key, fake_gateway):
    base, dash = no_key
    gw = fake_gateway({"/user/balance": lambda q: (200, balance("1.00"))})
    config.add({"name": "a", "gateway": "deepseek", "key": KEY, "url": gw.url})
    config.add({"name": "b", "gateway": "deepseek", "key": KEY, "url": gw.url})
    assert post(base, "/api/keys/select", {"name": "a"}) == (200, {"ok": True})
    assert dash.watched["name"] == "a" and config.load()["active"] == "a"
    old = dash.poller
    assert post(base, "/api/keys/remove", {"name": "a"}) == (200, {"ok": True})
    assert dash.watched["name"] == "b" and dash.poller is not old and old.stopped.is_set()
    assert post(base, "/api/keys/remove", {"name": "b"}) == (200, {"ok": True})
    assert dash.watched is None and dash.poller is None
    assert post(base, "/api/keys/select", {"name": "b"}) == (400, {"error": "no saved key named 'b'"})


def test_the_key_from_the_environment_is_listed_but_never_saved(dashboard):
    d = get(dashboard, "/api/state")[2]
    assert d["keys"] == [{"name": "KEYMETER_KEY", "gateway": "litellm", "key": "sk-tes…1234", "url": d["summary"]["gateway"],
                          "saved": False}]
    assert post(dashboard, "/api/keys/select", {"name": "KEYMETER_KEY"}) == (200, {"ok": True})
    assert post(dashboard, "/api/keys/remove", {"name": "KEYMETER_KEY"})[0] == 400
    assert not config.path().exists()


@pytest.mark.parametrize(("headers", "status"), [
    ({"Content-Type": "text/plain"}, 415),                  # what a form on another site can send
    ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
    ({"Origin": "https://evil.example"}, 403),
])
def test_key_changes_take_json_from_this_machine_only(no_key, headers, status):
    assert post(no_key[0], "/api/keys/add", {"gateway": "deepseek", "key": KEY}, headers)[0] == status
    assert config.load()["keys"] == []


def test_key_changes_are_off_beyond_loopback(no_key):
    base, dash = no_key
    assert dash.manage is True  # make_server() turns it off for any host but a loopback one
    dash.manage = False         # as on --host 0.0.0.0, without opening a port to the network in a test
    assert get(base, "/api/state")[2]["manage"] is False
    status, err = post(base, "/api/keys/add", {"gateway": "deepseek", "key": KEY})
    assert status == 403 and "keymeter add" in err["error"]


def test_a_big_or_odd_body_is_refused(no_key):
    base = no_key[0]
    assert post(base, "/api/keys/add", ["not", "an", "object"])[0] == 400
    # the size alone gets the answer, before any of the body is read (sending one would race the server closing)
    with socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])), timeout=5) as s:
        s.sendall(b"POST /api/keys/add HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 66560\r\n\r\n")
        assert s.makefile("rb").readline().split()[1:2] == [b"413"]


def test_beyond_loopback_the_page_learns_nothing_of_the_other_keys(no_key, fake_gateway):
    base, dash = no_key
    gw = fake_gateway({"/user/balance": lambda q: (200, balance("1.00"))})
    config.add({"name": "secret-project", "gateway": "litellm", "key": "sk-other-0123456789", "url": "http://internal.example"})
    config.add({"name": "ds", "gateway": "deepseek", "key": KEY, "url": gw.url})
    dash.start()
    dash.manage = False  # as on --host 0.0.0.0
    d = get(base, "/api/state")[2]
    assert [k["name"] for k in d["keys"]] == ["ds"]
    assert "secret-project" not in json.dumps(d) and "internal.example" not in json.dumps(d)
    config.path().write_text("{damaged", encoding="utf-8")
    assert get(base, "/api/state")[2]["keys_error"] is None  # no file paths for the network either
