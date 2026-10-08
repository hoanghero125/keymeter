import socket
import threading

import pytest
from conftest import KEY

from keymeter.gateways.base import classify
from keymeter.gateways.litellm import LiteLLM

INFO = {"key_alias": "dev", "spend": 1.5, "max_budget": 10, "team_id": None}


def test_reads_wrapped_key_info(fake_gateway):
    gw = fake_gateway({"/key/info": (200, {"key": "hashed", "info": INFO})})
    snap = LiteLLM(gw.url, KEY).fetch()
    assert snap.status == "OK"
    assert snap.key == INFO
    assert gw.requests == [("/key/info", {}, f"Bearer {KEY}")]


MISSING_KEY = (422, {"detail": [{"loc": ["query", "key"], "msg": "field required", "type": "value_error.missing"}]})


def test_falls_back_to_key_param_and_remembers_it(fake_gateway):
    # older LiteLLM versions require /key/info?key=... and answer 422 without it
    gw = fake_gateway({"/key/info": lambda q: (200, {"info": INFO}) if q.get("key") == KEY else MISSING_KEY})
    adapter = LiteLLM(gw.url, KEY)
    assert adapter.fetch().key == INFO
    assert adapter.fetch().key == INFO
    assert [q for _, q, _ in gw.requests] == [{}, {"key": KEY}, {"key": KEY}]


def test_retries_with_key_param_when_info_has_no_spend(fake_gateway):
    gw = fake_gateway({"/key/info": lambda q: (200, {"info": INFO}) if q.get("key") else (200, {"info": {"user_id": "u"}})})
    assert LiteLLM(gw.url, KEY).fetch().key == INFO


def test_reads_team_when_allowed(fake_gateway):
    gw = fake_gateway({
        "/key/info": (200, {"info": {**INFO, "team_id": "t1"}}),
        "/team/info": (200, {"team_id": "t1", "team_info": {"team_alias": "core", "spend": 4.0}, "keys": [{"key_alias": "dev"}]}),
    })
    snap = LiteLLM(gw.url, KEY).fetch()
    assert snap.team == {"team_alias": "core", "spend": 4.0}
    assert snap.team_keys == [{"key_alias": "dev"}]
    assert gw.requests[1][:2] == ("/team/info", {"team_id": "t1"})


def test_stops_asking_for_team_after_403(fake_gateway):
    gw = fake_gateway({
        "/key/info": (200, {"info": {**INFO, "team_id": "t1"}}),
        "/team/info": (403, {"error": {"message": "not allowed"}}),
    })
    adapter = LiteLLM(gw.url, KEY)
    snap = adapter.fetch()
    assert snap.status == "OK"
    assert snap.team_error == "HTTP 403: not allowed"
    assert snap.notices == ["this key can't read /team/info — showing key data only"]
    snap = adapter.fetch()
    assert (snap.team_error, snap.notices) == ("HTTP 403: not allowed", [])  # still explains the missing team
    assert [p for p, _, _ in gw.requests] == ["/key/info", "/team/info", "/key/info"]


def test_team_disabled(fake_gateway):
    gw = fake_gateway({"/key/info": (200, {"info": {**INFO, "team_id": "t1"}})})
    assert LiteLLM(gw.url, KEY, team=False).fetch().team_error == ""
    assert [p for p, _, _ in gw.requests] == ["/key/info"]


def test_error_statuses(fake_gateway):
    gw = fake_gateway({"/key/info": (401, {"error": {"message": "Authentication Error, Invalid proxy server token"}})})
    snap = LiteLLM(gw.url, KEY).fetch()
    assert (snap.status, snap.key) == ("AUTH ERROR", {})

    gw.routes["/key/info"] = (400, {"error": {"message": "Budget has been exceeded! Team=t1 Current cost: 10"}})
    assert LiteLLM(gw.url, KEY).fetch().status == "TEAM OVER BUDGET"


# LiteLLM checks the key's team on every route, /key/info included, so a blocked or over-budget team makes the poll
# itself fail; the message names what was refused first
@pytest.mark.parametrize(("status", "message", "expected"), [
    (401, "Authentication Error, Key is blocked. Update via `/key/unblock` if you're admin.", "BLOCKED"),
    (401, "Authentication Error, Team=t1 is blocked. Update via `/team/unblock` if you're an admin.", "TEAM BLOCKED"),
    (400, "Budget has been exceeded! Key=dev Current cost: 10.0, Max budget: 10.0", "OVER BUDGET"),
    (400, "Budget has been exceeded! Team=t1 Current cost: 51.0, Max budget: 50.0", "TEAM OVER BUDGET"),
    (400, "ExceededBudget: Team=t1 over 1d budget. Spend=$5.1000, Limit=$5.00", "TEAM OVER BUDGET"),
    (400, "Budget has been exceeded! User=u1 in Team=t1 Current cost: 5.0, Max budget: 5.0", "OVER BUDGET"),  # member budget
])
def test_error_status_names_the_scope(status, message, expected):
    assert classify(status, message) == expected


def test_network_error():
    snap = LiteLLM("http://127.0.0.1:9", KEY).fetch()  # nothing listens on the discard port
    assert snap.status == "OFFLINE"
    assert snap.message.startswith("network error:")


def test_non_http_answer_is_offline():
    srv = socket.create_server(("127.0.0.1", 0))

    def answer():  # something that isn't HTTP, like an SSH server on the wrong port
        conn, _ = srv.accept()
        conn.recv(1024)
        conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    snap = LiteLLM(f"http://127.0.0.1:{srv.getsockname()[1]}", KEY).fetch()
    srv.close()
    assert snap.status == "OFFLINE"
    assert snap.message.startswith("network error:")


@pytest.mark.parametrize("status", [400, 401, 404])
def test_never_puts_the_key_in_the_url_otherwise(fake_gateway, status):
    gw = fake_gateway({"/key/info": (status, {"detail": "Not Found"})})
    LiteLLM(gw.url, KEY).fetch()
    assert [q for _, q, _ in gw.requests] == [{}]
