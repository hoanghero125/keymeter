import csv

import pytest
from conftest import Scripted, ok

from keymeter.gateways import Snapshot
from keymeter.monitor import Monitor, derive_status, model_rows

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
    key = {"spend": 1.0, "model_spend": {"gpt": 0.9}, "model_max_budget": {"gpt": {"budget_limit": 1.0, "time_period": "1d"}}}
    mon = run(args, ok(T0, **key))
    assert "model gpt budget 90% used ($0.9000 of $1.000)" in texts(mon)


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
    ({"spend": 1}, {"spend": 50, "max_budget": 50}, "OVER BUDGET"),
    ({"spend": 1, "expires": "2020-01-01T00:00:00Z"}, {}, "EXPIRED"),
])
def test_derive_status(key, team, expected):
    assert derive_status(Snapshot(T0, "OK", key=key, team=team)) == expected


def test_over_budget_gets_a_message(args):
    mon = run(args, ok(T0, spend=10, max_budget=10))
    assert mon.snap.status == "OVER BUDGET"
    assert mon.snap.message == "gateway reports the key as over budget"


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
    assert rows[1][2:4] == ["1.0", "10"]


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
