from datetime import datetime, timezone

import pytest
from conftest import KEY

from keymeter.gateways.openrouter import OpenRouter, next_reset, translate


def ts(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(("period", "now", "expected"), [
    ("daily", ts(2026, 10, 7, 15, 30), datetime(2026, 10, 8, tzinfo=timezone.utc)),
    ("daily", ts(2026, 12, 31, 23, 59), datetime(2027, 1, 1, tzinfo=timezone.utc)),
    ("weekly", ts(2026, 10, 7, 15, 30), datetime(2026, 10, 12, tzinfo=timezone.utc)),   # Wednesday -> Monday
    ("weekly", ts(2026, 10, 12, 0, 0, 1), datetime(2026, 10, 19, tzinfo=timezone.utc)),  # Monday -> next Monday
    ("weekly", ts(2026, 10, 11, 23, 59), datetime(2026, 10, 12, tzinfo=timezone.utc)),   # Sunday -> Monday
    ("monthly", ts(2026, 10, 7, 15, 30), datetime(2026, 11, 1, tzinfo=timezone.utc)),
    ("monthly", ts(2026, 12, 15), datetime(2027, 1, 1, tzinfo=timezone.utc)),
    (None, ts(2026, 10, 7), None),
])
def test_next_reset_uses_utc_boundaries(period, now, expected):
    assert next_reset(period, now) == expected


def test_translate_monthly_limit():
    now = ts(2026, 10, 7, 12)
    k = translate({"label": "ci bot", "usage": 125.5, "usage_monthly": 25.5, "limit": 100, "limit_remaining": 74.5,
                   "limit_reset": "monthly", "expires_at": "2027-12-31T23:59:59Z"}, now)
    assert k == {
        "key_alias": "ci bot",
        "spend": 25.5,  # what counts against the limit this month, not the all-time usage
        "max_budget": 100.0,
        "budget_duration": "month",
        "budget_reset_at": "2026-11-01T00:00:00+00:00",
        "expires": "2027-12-31T23:59:59Z",
    }


def test_translate_without_limit_uses_all_time_usage():
    k = translate({"label": "sk-or-v1-au7...890", "usage": 12.25, "limit": None, "limit_remaining": None,
                   "limit_reset": "monthly", "expires_at": None}, ts(2026, 10, 7))
    assert k["spend"] == 12.25
    assert k["max_budget"] is None
    assert k["budget_duration"] is None and k["budget_reset_at"] is None
    assert k["key_alias"] is None  # the default label is only the masked key


def test_translate_exhausted_limit_reaches_budget():
    k = translate({"usage": 50, "limit": 20, "limit_remaining": 0, "limit_reset": None}, ts(2026, 10, 7))
    assert k["spend"] == k["max_budget"] == 20
    assert k["budget_reset_at"] is None


def test_fetch_success(fake_gateway):
    gw = fake_gateway({"/api/v1/key": (200, {"data": {"label": "sk-or-v1-x", "usage": 3.0, "limit": 10,
                                                      "limit_remaining": 7.0, "limit_reset": "daily"}})})
    snap = OpenRouter(gw.url + "/api", KEY).fetch()
    assert snap.status == "OK"
    assert snap.key["spend"] == 3.0 and snap.key["max_budget"] == 10
    assert gw.requests == [("/api/v1/key", {}, f"Bearer {KEY}")]


def test_fetch_auth_error(fake_gateway):
    gw = fake_gateway({"/api/v1/key": (401, {"error": {"message": "No auth credentials found", "code": 401}})})
    snap = OpenRouter(gw.url + "/api", KEY).fetch()
    assert snap.status == "AUTH ERROR"
    assert snap.message == "No auth credentials found"
    assert snap.key == {}
