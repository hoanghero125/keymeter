import csv
import json
import sys

import pytest
from conftest import Scripted, ok

from keymeter.gateways import Snapshot
from keymeter.monitor import Monitor, budget_view, derive_status, model_rows
from keymeter.util import tidy

T0 = 1_800_000_000.0


def run(args, *snaps):
    mon = Monitor(Scripted(*snaps), args)
    for _ in snaps:
        mon.poll()
    return mon


def texts(mon):
    return [text for _, _, text in mon.events]


def test_burn_rate_and_projections(args):
    mon = run(args, ok(T0, spend=1.0, max_budget=10), ok(T0 + 600, spend=1.5, max_budget=10))
    ki = mon.summary()["key_info"]
    assert ki["burn_per_hour"] == pytest.approx(3.0)       # $0.50 in 10 minutes
    assert ki["remaining"] == pytest.approx(8.5)
    assert ki["runs_out_in_s"] == pytest.approx(8.5 / 3.0 * 3600)
    assert ki["session_spend"] == pytest.approx(0.5)
    assert ki["used_pct"] == pytest.approx(15.0)


def test_burn_rate_needs_two_samples(args):
    ki = run(args, ok(T0, spend=1.0, max_budget=10)).summary()["key_info"]
    assert ki["burn_per_hour"] is None and ki["runs_out_in_s"] is None


def test_burn_rate_window_drops_old_samples(args):
    # a poll every 5 minutes: slow spend for 25 minutes, then $0.50 per poll
    spends = [0.1 * i if i <= 5 else 0.5 + 0.5 * (i - 5) for i in range(11)]
    mon = run(args, *(ok(T0 + 300 * i, spend=s) for i, s in enumerate(spends)))
    # the 15-minute window is anchored on the last sample at or before its start (T0+2100): $1.50 in 15 minutes
    assert mon.samples[0][0] == T0 + 2100
    assert mon.rate(1) == pytest.approx(6.0)


def test_projection_at_reset(args):
    reset = "2027-01-15T08:00:00+00:00"
    mon = run(args, ok(T0, spend=2.0, max_budget=10, budget_reset_at=reset),
              ok(T0 + 3600, spend=3.0, max_budget=10, budget_reset_at=reset))
    ki = mon.summary()["key_info"]
    assert ki["reset_in_s"] > 0
    assert ki["projected_at_reset"] == pytest.approx(3.0 + ki["reset_in_s"] / 3600, rel=1e-3)


def test_budget_reset_restarts_tracking(args):
    mon = run(args, ok(T0, spend=9.0), ok(T0 + 60, spend=9.5), ok(T0 + 120, spend=0.1))
    assert "spend went down — budget period reset, restarting burn-rate tracking" in texts(mon)
    assert mon.rate(1) is None
    assert mon.summary()["key_info"]["session_spend"] == 0.0


def test_thresholds_alert_once_per_level(args):
    mon = run(args, *(ok(T0 + i * 60, spend=s, max_budget=10) for i, s in enumerate([7.0, 8.5, 8.7, 9.6, 9.7])))
    alerts = [(lvl, text) for _, lvl, text in mon.events if "budget" in text]
    assert alerts == [("warn", "key budget 85% used ($8.500 of $10.000)"),
                      ("crit", "key budget 96% used ($9.600 of $10.000)")]
    assert mon.bell


def test_model_budget_threshold(args):
    key = {"spend": 1.0, "model_spend": {"gpt": 0.9}, "model_max_budget": {"gpt": {"budget_limit": 1.0, "time_period": "1d"}},
           "model_max_budget_usage": {"gpt": {"current_spend": 0.9, "budget_limit": 1.0, "time_period": "1d"}}}
    mon = run(args, ok(T0, **key))
    assert "model gpt budget 90% used ($0.9000 of $1.000)" in texts(mon)


def test_model_limit_without_period_spend_shows_the_limit_alone(args):
    # LiteLLM before 1.90 has no per-period spend per model; all-time model_spend says nothing about a limit that resets
    key = {"spend": 5.0, "model_spend": {"gpt": 5.0}, "model_max_budget": {"gpt": {"budget_limit": 1.0, "time_period": "1d"}}}
    mon = run(args, ok(T0, **key))
    r = mon.summary()["models"][0]
    assert (r["limit"], r["period"], r["period_spend"], r["used_pct"]) == (1.0, "1d", None, None)
    assert not any(text.startswith("model gpt") for text in texts(mon))


def test_model_rows_take_limits_from_usage_when_the_key_has_none():
    # a key on a budget tier has no model_max_budget of its own, but /key/info still reports the tier's per-model usage
    rows = model_rows({"model_spend": {"gpt": 2.0}, "model_max_budget": {},
                       "model_max_budget_usage": {"gpt": {"current_spend": 0.5, "budget_limit": 2.0, "time_period": "30d"}}}, {})
    assert (rows[0]["limit"], rows[0]["period"], rows[0]["period_spend"], rows[0]["used_pct"]) == (2.0, "30d", 0.5, 25.0)


def test_status_changes_and_backoff(args):
    down = Snapshot(T0, "OFFLINE", "network error: refused")
    mon = Monitor(Scripted(ok(T0, spend=1.0), down, down, down, down, ok(T0 + 300, spend=1.0)), args)
    mon.poll()
    assert mon.next_delay() == 15
    delays = []
    for _ in range(4):
        mon.poll()
        delays.append(mon.next_delay())
    assert delays == [15, 30, 60, 60]
    assert (mon.fails, mon.fail_streak) == (4, 4)
    assert mon.summary()["key_info"]["spend"] == 1.0  # still shows the last good data
    mon.bell = False
    mon.poll()
    assert mon.fail_streak == 0
    assert texts(mon)[-1] == "OFFLINE → OK, gateway is usable again" and mon.bell
    assert "OK → OFFLINE: network error: refused" in texts(mon)


def test_notices_and_new_models_become_events(args):
    first = ok(T0, spend=1.0, model_spend={"a": 1.0})
    first.notices.append("this key can't read /team/info — showing key data only")
    mon = run(args, first, ok(T0 + 60, spend=1.2, model_spend={"a": 1.0, "b": 0.2}))
    assert "this key can't read /team/info — showing key data only" in texts(mon)
    assert "new model in use: b" in texts(mon)


@pytest.mark.parametrize(("key", "team", "expected"), [
    ({"spend": 1, "max_budget": 10}, {}, "OK"),
    ({"spend": 1, "blocked": True}, {}, "BLOCKED"),
    ({"spend": 1}, {"blocked": True}, "TEAM BLOCKED"),
    ({"spend": 10, "max_budget": 10}, {}, "OVER BUDGET"),
    ({"max_budget": 0}, {}, "OVER BUDGET"),  # no spend reported counts as $0, as in budget_view
    ({"spend": 1, "max_budget": 10}, {"spend": 49.9, "max_budget": 50}, "OK"),
    ({"spend": 1, "max_budget": 10}, {"spend": 50, "max_budget": 50}, "OK"),  # LiteLLM refuses a team only once over its budget
    ({"spend": 1, "max_budget": 10}, {"spend": 0, "max_budget": 0}, "OK"),
    ({"spend": 1, "max_budget": 10}, {"spend": 50.5, "max_budget": 50}, "TEAM OVER BUDGET"),
    ({"spend": 1, "expires": "2020-01-01T00:00:00Z"}, {}, "EXPIRED"),
])
def test_derive_status(key, team, expected):
    assert derive_status(Snapshot(T0, "OK", key=key, team=team)) == expected


@pytest.mark.parametrize(("team", "status", "message"), [
    ({"spend": 50, "max_budget": 50}, "OK", ""),  # nothing left, but LiteLLM still serves a team at its budget
    ({"spend": 0, "max_budget": 0}, "OK", ""),
    ({"max_budget": 0}, "OK", ""),
    ({"spend": 50.5, "max_budget": 50}, "TEAM OVER BUDGET", "gateway reports the team as over budget"),
    ({"spend": 1, "max_budget": -5}, "TEAM OVER BUDGET", "gateway reports the team as over budget"),
])
def test_used_up_team_status_follows_litellm(args, team, status, message):
    s = run(args, Snapshot(T0, "OK", key={"spend": 1, "max_budget": 10}, team=team)).summary()
    assert s["team_info"]["runs_out_in_s"] == 0.0
    assert (s["status"], s["message"]) == (status, message)


@pytest.mark.parametrize(("key", "team", "message"), [
    ({"spend": 10, "max_budget": 10}, {}, "gateway reports the key as over budget"),
    ({"spend": 1, "max_budget": 10}, {"spend": 51, "max_budget": 50}, "gateway reports the team as over budget"),
    ({"spend": 1}, {"blocked": True}, "gateway reports the team as blocked"),
    ({"spend": 1, "expires": "2020-01-01T00:00:00Z"}, {}, "gateway reports the key as expired"),
])
def test_status_message_names_the_scope(args, key, team, message):
    assert run(args, Snapshot(T0, "OK", key=key, team=team)).snap.message == message


def test_model_rows_join_spend_and_limits():
    rows = model_rows({"model_spend": {"a": 3.0, "b": 1.0}, "model_max_budget": {"b": 2.0},
                       "model_max_budget_usage": {"b": {"current_spend": 0.5}}}, {"model:a": 2.0})
    assert [r["model"] for r in rows] == ["a", "b"]
    assert rows[0]["share_pct"] == 75.0 and rows[0]["session_spend"] == 1.0
    assert rows[1]["limit"] == 2.0 and rows[1]["period_spend"] == 0.5 and rows[1]["used_pct"] == 25.0


def test_summary_describes_the_gateway_and_masks_the_key(args):
    s = run(args, ok(T0, spend=1.0)).summary()
    assert s["gateway"] == "http://gateway.test"
    assert s["gateway_type"] == "litellm"
    assert s["features"] == ["limits", "models", "team"]
    assert s["key"] == "sk-tes…1234"
    assert "abcdefghijkl" not in repr(s)


def test_csv_log(args, tmp_path):
    args.log = str(tmp_path / "polls.csv")
    run(args, ok(T0, spend=1.0, max_budget=10), Snapshot(T0 + 60, "OFFLINE", "down"))
    with open(args.log, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0][:3] == ["time", "status", "key_spend"]
    assert [r[1] for r in rows[1:]] == ["OK", "OFFLINE"]
    assert rows[1][2:4] == ["1.0", "10.0"]


class Broken:
    """An adapter whose first fetch blows up, like a gateway answer the monitor can't read."""

    name, url, key, features = "litellm", "http://gateway.test", "sk-test-abcdefghijkl1234", frozenset()

    def __init__(self):
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return ok(T0 + 60, spend=1.0)


def test_a_failing_poll_does_not_raise(args):
    mon = Monitor(Broken(), args)
    assert mon.poll().status == "ERROR"
    assert mon.snap.message == "RuntimeError: boom" and mon.fail_streak == 1
    assert mon.poll().status == "OK"


def test_backoff_never_overflows(args):
    mon = Monitor(Scripted(), args)
    mon.fail_streak = 5000
    assert mon.next_delay() == 60


def test_log_errors_become_one_event(args, tmp_path):
    args.log = str(tmp_path / "missing-dir" / "polls.csv")
    mon = run(args, ok(T0, spend=1.0), ok(T0 + 60, spend=1.1))
    assert mon.snap.status == "OK"
    assert sum(text.startswith("can't write to") for text in texts(mon)) == 1


def test_zero_budget_is_a_budget(args):
    ki = run(args, ok(T0, spend=0.0, max_budget=0)).summary()["key_info"]
    assert (ki["remaining"], ki["used_pct"]) == (0.0, 100.0)


def test_runs_out_now_when_budget_is_used_up(args):
    ki = run(args, ok(T0, spend=9.0, max_budget=10), ok(T0 + 600, spend=10.0, max_budget=10)).summary()["key_info"]
    assert ki["burn_per_hour"] > 0 and ki["runs_out_in_s"] == 0


def test_used_up_budget_runs_out_now_whatever_the_rate(args):
    used_up = {"key": {"spend": 10.0, "max_budget": 10}, "team": {"spend": 60.0, "max_budget": 50}}
    mon = Monitor(Scripted(Snapshot(T0, "OK", **used_up), Snapshot(T0 + 600, "OK", **used_up)), args)
    for rate in (None, 0):  # no rate after one poll; then 0, since a refused key stops spending
        mon.poll()
        s = mon.summary()
        assert s["key_info"]["burn_per_hour"] == rate
        assert s["key_info"]["runs_out_in_s"] == 0.0 and s["team_info"]["runs_out_in_s"] == 0.0


def test_negative_budget_is_used_up(args):
    # the gateway refuses spend >= max_budget, so a negative budget is over budget and used up; its % and remaining mean nothing
    s = run(args, ok(T0, spend=1.0, max_budget=-5), ok(T0 + 600, spend=1.0, max_budget=-5)).summary()
    ki = s["key_info"]
    assert s["status"] == "OVER BUDGET"
    assert (ki["max_budget"], ki["remaining"], ki["used_pct"], ki["burn_per_hour"], ki["runs_out_in_s"]) == (-5.0, None, None, 0.0, 0.0)


@pytest.mark.parametrize("rate", [None, 0.0, 2.0])
def test_negative_budget_runs_out_now_whatever_the_rate(rate):
    # unlike a budget >= 0, remaining stays None, so runs_out_in_s 0 alone is what tells the UIs it is used up
    v = budget_view({"spend": 1.0, "max_budget": -5}, rate, T0, None)
    assert (v["remaining"], v["used_pct"], v["runs_out_in_s"]) == (None, None, 0.0)


def test_a_run_out_under_half_a_second_is_not_used_up(args):
    # $0.001 left at ~$10/h runs out in 0.36 s; tidy() rounds to whole seconds, and 0 would read as used up
    s = tidy(run(args, ok(T0, spend=0.0, max_budget=10), ok(T0 + 3600, spend=9.999, max_budget=10)).summary())
    assert (s["status"], s["key_info"]["remaining"], s["key_info"]["runs_out_in_s"]) == ("OK", 0.001, 1)


def test_a_budget_not_quite_used_up_keeps_some_remaining(args):
    # 100 requests at $0.10 add up to $9.99999999999998: LiteLLM still serves the key, and tidy() rounds to 6 decimals
    s = tidy(run(args, ok(T0, spend=9.0, max_budget=10), ok(T0 + 600, spend=9.99999999999998, max_budget=10)).summary())
    assert s["status"] == "OK" and s["key_info"]["remaining"] > 0 and s["key_info"]["runs_out_in_s"] >= 1


@pytest.mark.parametrize("snaps", [
    (ok(T0, spend=0.0, max_budget=1e9), ok(T0 + 600, spend=1e-300, max_budget=1e9)),  # runs out in inf seconds
    (ok(T0, spend=0.0, max_budget=sys.float_info.max), ok(T0 + 3600, spend=1.0, max_budget=sys.float_info.max)),
    (ok(T0, spend="nan", max_budget=10), ok(T0 + 600, spend="nan", max_budget=10)),
], ids=["tiny spend step", "budget near float max", "spend not a number"])
def test_tidy_takes_numbers_that_are_not_finite(args, snaps):
    # tidy() feeds `keymeter web` and `keymeter json`: raising would stop the web poller, and JSON has no NaN or Infinity
    s = tidy(run(args, *snaps).summary())
    json.dumps(s, allow_nan=False)  # what the page can parse
    assert s["status"] == "OK" and s["key_info"]["runs_out_in_s"] is None


@pytest.mark.parametrize("limit", ["inf", "nan", "-inf"])  # JSON's Infinity and NaN
def test_a_model_limit_that_is_not_finite_is_no_limit(limit):
    for key in ({"model_max_budget": {"gpt": {"budget_limit": limit, "time_period": "1d"}}}, {"model_max_budget": {"gpt": limit}},
                {"model_max_budget_usage": {"gpt": {"current_spend": 0.5, "budget_limit": limit, "time_period": "1d"}}}):
        r = model_rows({"model_spend": {"gpt": 1.0}, **key}, {})[0]
        assert (r["limit"], r["used_pct"]) == (None, None)


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), float("-inf")])  # JSON's NaN and Infinity
def test_a_budget_that_is_not_finite_is_no_budget(args, budget):
    snaps = [Snapshot(T0 + 600 * i, "OK", key={"spend": spend, "max_budget": budget}, team={"spend": spend, "max_budget": budget},
                      team_keys=[{"key_alias": "dev", "spend": spend, "max_budget": budget}]) for i, spend in enumerate([1.0, 1.5])]
    mon = run(args, *snaps)
    s = tidy(mon.summary())
    json.dumps(s, allow_nan=False)  # what the page can parse
    assert s["status"] == "OK"
    for v in (s["key_info"], s["team_info"]):
        assert (v["max_budget"], v["remaining"], v["used_pct"], v["runs_out_in_s"]) == (None, None, None, None)
    assert (s["team_keys"][0]["max_budget"], s["team_keys"][0]["used_pct"]) == (None, None)
    assert not any("budget" in text for text in texts(mon))


def test_odd_model_fields_do_not_break_the_summary(args):
    # LiteLLM sends dicts here; anything else from an odd gateway or proxy must not crash summary() (and the web poller)
    mon = run(args, ok(T0, spend=1.0, model_spend=["gpt"], model_max_budget=["gpt"], model_max_budget_usage=["gpt"]))
    assert mon.snap.status == "OK"
    assert mon.summary()["models"] == []


@pytest.mark.parametrize(("keys", "rows"), [
    (5, []), (True, []), ({"a": 1}, []),
    ([{"key_name": 5, "spend": 1.0}], ["5"]), ([{"token": 12345, "spend": 1.0}], ["12345"]),
])
def test_odd_team_keys_do_not_break_the_summary(args, keys, rows):
    # LiteLLM sends a list of key dicts; anything else must not crash summary(), which runs outside poll()'s try (json, tui)
    mon = run(args, Snapshot(T0, "OK", key={"spend": 1.0, "team_id": "t1"}, team={"spend": 1.0}, team_keys=keys))
    assert [r["key"] for r in mon.summary()["team_keys"]] == rows


def test_csv_log_defuses_formulas(args, tmp_path):
    args.log = str(tmp_path / "polls.csv")
    run(args, Snapshot(T0, "HTTP 500", '=HYPERLINK("http://x.invalid","click")'))
    with open(args.log, newline="", encoding="utf-8") as f:
        row = list(csv.reader(f))[1]
    assert row[-1] == "'" + '=HYPERLINK("http://x.invalid","click")'


def test_csv_log_writes_only_numbers_in_number_columns(args, tmp_path):
    args.log = str(tmp_path / "polls.csv")
    formula = '=HYPERLINK("http://x.invalid","click")'
    snap = Snapshot(T0, "OK", key={"spend": formula, "max_budget": "@SUM(1)"}, team={"spend": "+1+1", "max_budget": "50"})
    run(args, snap)
    with open(args.log, newline="", encoding="utf-8") as f:
        row = list(csv.reader(f))[1]
    assert row[2:6] == ["", "", "", "50.0"]


def test_csv_log_leaves_numbers_that_are_not_finite_empty(args, tmp_path):
    args.log = str(tmp_path / "polls.csv")
    run(args, Snapshot(T0, "OK", key={"spend": "nan", "max_budget": "inf"}, team={"spend": 1.0, "max_budget": float("-inf")}))
    with open(args.log, newline="", encoding="utf-8") as f:
        row = list(csv.reader(f))[1]
    assert row[2:6] == ["", "", "1.0", ""]
