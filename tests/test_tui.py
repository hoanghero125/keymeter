import io
import time
from datetime import datetime, timezone

import pytest
from conftest import Scripted, ok
from rich.console import Console
from rich.live_render import LiveRender
from rich.text import Text

from keymeter.gateways import Snapshot
from keymeter.monitor import Monitor
from keymeter.tui import fit_rest, fit_tables, render, render_budget, render_burn, render_header

T0 = 1_800_000_000.0


def text_of(renderable, width=120):
    console = Console(file=io.StringIO(), width=width, record=True)
    console.print(renderable)
    return console.export_text()


def polled(args, *snaps):
    mon = Monitor(Scripted(*snaps), args)
    for _ in snaps:
        mon.poll()
    return mon


def iso_in(seconds):
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


def team_snap(ts, key_spend, team_spend):
    reset = iso_in(3 * 86400)
    limits = {"rpm_limit": 1000, "tpm_limit": 10_000_000, "max_parallel_requests": 50}
    models = {"vertex_ai/gemini-2.5-pro-preview-06-05": key_spend * 0.6, "openai/gpt-4o": key_spend * 0.4}
    return Snapshot(ts, "OK",
                    key={"spend": key_spend, "max_budget": 100, "budget_reset_at": reset, "budget_duration": "30d", "team_id": "t1",
                         "model_spend": models, **limits},
                    team={"spend": team_spend, "max_budget": 7500, "budget_reset_at": reset, "team_alias": "research-team",
                          **{k: v * 100 for k, v in limits.items()}},
                    team_keys=[{"key_alias": f"key-{i}", "spend": i * 10.0, "max_budget": 1000} for i in range(3)])


def test_used_up_budget_says_so(args):
    mon = Monitor(Scripted(ok(T0, spend=9.0, max_budget=10), ok(T0 + 600, spend=10.0, max_budget=10)), args)
    mon.poll()
    mon.poll()
    out = text_of(render_burn(mon.summary(), args, T0 + 600))
    assert "used up" in out and "not at this rate" not in out


@pytest.mark.parametrize("spends", [(10.0,), (10.0, 10.0)], ids=["before a rate is measured", "at a zero rate"])
def test_used_up_comes_before_measuring_and_not_at_this_rate(args, spends):
    mon = polled(args, *(ok(T0 + 600 * i, spend=sp, max_budget=10) for i, sp in enumerate(spends)))
    line = next(ln for ln in text_of(render_burn(mon.summary(), args, T0)).splitlines() if "Runs out in" in ln)
    assert "used up" in line


@pytest.mark.parametrize(("budget", "spent"), [(1e9, 1e-6), (2000, 1.0)], ids=["past the year 3000", "two weeks away"])
def test_run_out_clock_far_away_is_left_out(args, budget, spent):
    # a weekday more than a week away is ambiguous; a tiny burn on a budget that never resets runs out past the year
    # 3000, which Windows can't convert at all
    mon = polled(args, ok(T0, spend=0.0, max_budget=budget), ok(T0 + 600, spend=spent, max_budget=budget))
    line = next(ln for ln in text_of(render_burn(mon.summary(), args, T0 + 600)).splitlines() if "Runs out in" in ln)
    assert "d " in line and "·" not in line


def test_run_out_clock_that_cant_be_formatted_is_left_out(args, monkeypatch):
    s = polled(args, ok(T0, spend=9.0, max_budget=100), ok(T0 + 600, spend=9.5, max_budget=100)).summary()

    def localtime(ts=None):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(time, "localtime", localtime)
    line = next(ln for ln in text_of(render_burn(s, args, T0 + 600)).splitlines() if "Runs out in" in ln)
    assert "1d 6h" in line and "·" not in line


@pytest.mark.parametrize("width", [60, 80, 100, 110, 120, 125, 130, 140, 200])
def test_values_are_never_cut_off(args, width):
    mon = polled(args, team_snap(time.time() - 1200, 10.0, 7000.0), team_snap(time.time(), 10.5, 7241.0))
    s = mon.summary()
    console = Console(file=io.StringIO(), width=width, record=True)
    console.print(render(mon, console, once=True))
    out = console.export_text().replace(s["key"], "")  # the masked key has a "…" of its own
    assert "…" not in out
    assert "Projected at reset" in out and "$7,241.00" in out and "research-team" in out


def wide_snap(ts, key_spend, team_spend):
    """team_snap with wider values: a long model name, $10M budgets on long team-key names, and huge rate limits."""
    snap = team_snap(ts, key_spend, team_spend)
    snap.key.update(model_spend={"bedrock/us.anthropic.claude-3-7-sonnet-20250219-v1:0": key_spend}, tpm_limit=10**12)
    snap.team.update(tpm_limit=10**14)
    snap.team_keys = [{"key_alias": f"alice-dev-project-x-{i}", "spend": 2469.12 + i * 1000, "max_budget": 10_000_000} for i in range(4)]
    return snap


def test_wide_values_are_never_cut_off_or_split(args):
    mon = polled(args, wide_snap(time.time() - 1200, 10.0, 7000.0), wide_snap(time.time(), 10.5, 7241.0))
    key = mon.summary()["key"]
    for width in range(60, 201):
        out = text_of(render(mon, Console(width=width), once=True), width).replace(key, "")
        assert "…" not in out, width
        assert out.count("$10,000,000.00") == 4, width  # table numbers stay on one line; the names wrap instead


def test_overdue_reset_counts_as_now(args):
    mon = polled(args, ok(time.time(), spend=5.0, max_budget=10, budget_reset_at=iso_in(-60)))
    out = text_of(render_budget(mon.summary(), args))
    assert "resets in 0s" in out and "resets in -" not in out


@pytest.mark.parametrize(("reset_in", "after"), [(60, True), (-60, False)], ids=["reset ahead", "reset overdue"])
def test_run_out_after_reset_only_when_the_reset_is_ahead(args, reset_in, after):
    # LiteLLM resets budgets from a job that runs every few minutes, so an overdue one can still come after the key runs out
    mon = polled(args, *(ok(T0 + 600 * i, spend=sp, max_budget=10, budget_reset_at=iso_in(reset_in)) for i, sp in enumerate((5.0, 9.0))))
    line = next(ln for ln in text_of(render_burn(mon.summary(), args, T0 + 600)).splitlines() if "Runs out in" in ln)
    assert "2m 30s" in line and ("(after reset)" in line) == after


def test_team_error_is_shown(args):
    snap = ok(time.time(), spend=1.0, team_id="t1")
    snap.team_error = "HTTP 500: Internal Server Error"
    mon = polled(args, snap)
    out = text_of(render_header(mon, mon.summary(), time.time()))
    assert "team t1" in out and "HTTP 500: Internal Server Error" in out


@pytest.mark.parametrize(("team", "shown"), [
    ("team info refused", "HTTP 401: Authentication Error"),
    ("team info refused by a proxy", "HTTP 403: <html> <head><title>403 Forbidden</title>"),
])
def test_long_team_error_keeps_to_one_line(args, team, shown):
    # LiteLLM explains a refused /team/info at length, a proxy in front of it in an HTML page (line breaks and all), and
    # the error now stays for good: it must not take over the header
    mon = key_with(args, team, None)
    lines = text_of(render_header(mon, mon.summary(), time.time()), 80).splitlines()
    assert len(lines) == 5  # border, status, poll line, the error, border
    assert "team t1" in lines[1] and f"team info unavailable: {shown}" in lines[3]


def test_once_shows_the_whole_team_error(args):
    # --once has no screen to protect, so the full error is printed (live keeps it to one line)
    mon = key_with(args, "team info refused", None)
    out = "".join(text_of(render_header(mon, mon.summary(), time.time(), once=True), 80).replace("│", "").split())
    assert "".join(TEAM_REFUSED.split()) in out


def test_gateway_text_is_not_read_as_markup(args):
    names = {"weird[/]name": 2.0, "vertex/[bold]x": 1.0}
    snap = ok(time.time(), spend=3.0, max_budget=10, key_alias="[/]alias", team_id="t1", rpm_limit="[red]lots",
              model_spend=names, model_max_budget={"weird[/]name": 5})
    snap.team = {"spend": 1.0, "team_alias": "[b]team", "tpm_limit": "x[/]"}
    snap.team_keys = [{"key_alias": "[/]k", "spend": 1.0}]
    mon = polled(args, snap)
    console = Console(file=io.StringIO(), width=200, record=True)
    console.print(render(mon, console, once=True))
    out = console.export_text()
    for text in (*names, "[/]alias", "[red]lots", "[b]team", "x[/]", "[/]k"):
        assert text in out


def test_budget_without_a_usable_percentage(args):
    mon = polled(args, ok(time.time(), spend=1.0, max_budget=-5, model_spend={"m": 1.0}, model_max_budget={"m": 10}))
    s = mon.summary()
    assert s["key_info"]["used_pct"] is None
    s["models"][0].update(used_pct=None, period_spend=None)  # a per-model limit without this period's spend
    out = text_of(render_budget(s, args))
    assert "$1.000 spent · budget" in out and "limit $10.000  m" in out
    console = Console(file=io.StringIO(), width=120, record=True)
    console.print(render(mon, console, once=True))


def many(args, models, keys):
    snap = Snapshot(time.time(), "OK",
                    key={"spend": 1.0, "max_budget": 10, "team_id": "t1", "model_spend": {f"model-{i}": float(i) for i in range(models)}},
                    team={"spend": 5.0, "max_budget": 50}, team_keys=[{"key_alias": f"key-{i}", "spend": float(i)} for i in range(keys)])
    mon = polled(args, snap)
    mon.events.extend((time.time(), "info", f"event {i}") for i in range(8))
    return mon


@pytest.mark.parametrize(("width", "height"), [(80, 60), (120, 50), (200, 40)])
def test_live_view_fits_the_screen(args, width, height):
    mon = many(args, 30, 20)
    console = Console(file=io.StringIO(), width=width, height=height, record=True)
    console.print(render(mon, console))
    lines = console.export_text().splitlines()
    out = "\n".join(lines)
    assert len(lines) <= height
    assert "model-29" in out and "key-19" in out and "… and" in out  # top spenders first, the rest counted
    assert "event 0" in out and "Ctrl+C to quit" in out


def test_live_view_shows_everything_that_fits(args):
    mon = many(args, 30, 20)
    console = Console(file=io.StringIO(), width=200, height=200, record=True)
    console.print(render(mon, console))
    out = console.export_text()
    assert "model-0" in out and "key-0" in out and "… and" not in out


def test_short_screen_drops_the_tables_then_the_oldest_events(args):
    mon = many(args, 30, 20)
    console = Console(file=io.StringIO(), width=120, height=26, record=True)
    console.print(render(mon, console))
    lines = console.export_text().splitlines()
    out = "\n".join(lines)
    assert len(lines) <= 26
    assert "event 7" in out and "event 0" not in out and "Ctrl+C to quit" in out and "RPM limit" in out
    s = mon.summary()
    assert text_of(fit_tables(console, s, args, 3)[0]).startswith("spend by model and team keys hidden")
    assert fit_tables(console, s, args, 0) == []


def test_hidden_note_is_left_out_when_it_does_not_fit(args):
    # at 60 columns the note takes two lines, so one line of room would push the footer off the screen
    s = many(args, 30, 20).summary()
    console = Console(width=60)
    assert fit_tables(console, s, args, 1) == [] and len(fit_tables(console, s, args, 2)) == 1
    note = text_of(fit_tables(console, s, args, 9, ["limits", "spend per poll"])[0], 200)
    assert note.startswith("limits, spend per poll, spend by model and team keys hidden")


def test_narrow_live_view_never_runs_past_the_screen(args):
    mon = many(args, 30, 20)
    for height in range(30, 46):  # some of these leave a single line for the two-line note
        console = Console(file=io.StringIO(), width=60, height=height, record=True)
        console.print(render(mon, console))
        assert len(console.export_text().splitlines()) <= height, height


def team_trouble(args, trouble):
    snaps = [team_snap(time.time() - 1200, 10.0, 7000.0), team_snap(time.time(), 10.5, 7241.0)]
    if trouble == "gateway offline":  # two more header lines: the error, and that the numbers are old
        snaps.append(Snapshot(time.time(), "OFFLINE", "connection refused"))
    else:
        for snap in snaps:
            snap.key["model_max_budget"] = {"openai/gpt-4o": 50, "vertex_ai/gemini-2.5-pro-preview-06-05": 80}
    return polled(args, *snaps)


@pytest.mark.parametrize("trouble", ["gateway offline", "two model limits"])
def test_team_key_fits_a_120x30_screen_limits_and_all(args, trouble):
    # Windows Terminal opens at 120x30; budget and burn rate sit side by side there, even with a team: their values wrap,
    # since stacking the two panels would push the limits off this screen
    mon = team_trouble(args, trouble)
    console = Console(file=io.StringIO(), width=120, height=30, record=True)
    console.print(render(mon, console))
    lines = console.export_text().splitlines()
    out = "\n".join(lines)
    assert len(lines) <= 30
    assert "RPM limit" in out and "Events (newest first)" in out and "Ctrl+C to quit" in out


@pytest.mark.parametrize(("width", "height"), [(80, 24), (100, 30)])
@pytest.mark.parametrize("trouble", ["gateway offline", "two model limits"])
def test_short_screen_drops_the_limits_row_before_the_last_event(args, trouble, width, height):
    mon = team_trouble(args, trouble)
    console = Console(file=io.StringIO(), width=width, height=height, record=True)
    console.print(render(mon, console))
    lines = console.export_text().splitlines()
    out = "\n".join(lines)
    assert len(lines) <= height
    assert "Runs out in" in out and "Events (newest first)" in out and "Ctrl+C to quit" in out
    assert "RPM limit" not in out


REFUSED = "network error: [WinError 10061] No connection could be made because the target machine actively refused it"
TEAM_REFUSED = ("HTTP 401: Authentication Error, Only proxy admin can be used to generate, delete, update info for new "
                "keys/users/teams. Route=/team/info. Your role=internal_user. Your user_id=alice")[:160]
NGINX_PAGE = ("<html>\r\n<head><title>{0}</title></head>\r\n<body>\r\n<center><h1>{0}</h1></center>\r\n"
              "<hr><center>nginx</center>\r\n</body>\r\n</html>")  # a body that isn't JSON, as http_get keeps it
TRACEBACK = "Internal Server Error, Traceback (most recent call last):\n" + "".join(
    f'  File "/usr/lib/python3.13/site-packages/litellm/proxy/proxy_server.py", line {n}, in info_key_fn\n    raise e\n'
    for n in range(1400, 1420))
FAILURES = {  # a failed poll: its status, its message, and the part of the message the header shows on one line
    "refused": ("OFFLINE", REFUSED, "actively refused it"),
    "proxy error page": ("HTTP 502", NGINX_PAGE.format("502 Bad Gateway"), "<head><title>502 Bad Gateway</title></head>"),
    "traceback": ("HTTP 500", TRACEBACK, "Internal Server Error, Traceback (most recent call last):"),
}


def key_with(args, team, failure, limits=0):
    snaps = [team_snap(time.time() - 1200, 10.0, 7000.0), team_snap(time.time(), 10.5, 7241.0)]
    for snap in snaps:
        if limits:  # per-model budgets, a "Model" row of the Budget panel each
            names = [*snap.key["model_spend"], *(f"openai/gpt-4.1-mini-{i}" for i in range(limits))]
            snap.key["model_max_budget"] = {m: 50 + i for i, m in enumerate(names[:limits])}
        if team != "team":
            snap.team, snap.team_keys = {}, []
        if team == "no team":
            del snap.key["team_id"]
        elif team == "team info refused":  # kept on every poll once /team/info answers 401 or 403
            snap.team_error = TEAM_REFUSED
        elif team == "team info refused by a proxy":  # as litellm.py words it
            snap.team_error = f"HTTP 403: {NGINX_PAGE.format('403 Forbidden')}"[:160]
    if failure:  # Windows takes about 2 s to refuse a connection; a second failure backs off
        snaps += [Snapshot(time.time(), *FAILURES[failure][:2], 2034.0) for _ in range(2)]
    mon = polled(args, *snaps)
    mon.next_poll_at = time.time() + mon.next_delay()  # as run_live sets it
    return mon


def live_frame(mon, width, height):
    """The live view as rich Live draws it: a frame taller than the screen loses its last lines to a "..." line."""
    console = Console(file=io.StringIO(), width=width, height=height, record=True)
    console.print(LiveRender(render(mon, console)))
    return console.export_text().splitlines()


@pytest.mark.parametrize(("width", "height"), [(80, 24), (100, 30), (120, 30), (132, 43)])
@pytest.mark.parametrize("team", ["team", "team info refused", "team info refused by a proxy", "no team"])
@pytest.mark.parametrize("failure", [*FAILURES, None], ids=[*FAILURES, "ok"])
def test_live_frame_is_never_cropped(args, team, failure, width, height):
    # offline, the header gains the error (two lines of it from Windows), that the numbers are old and a backing-off note
    # that wraps the poll line: on 80x24 that made a 25-line frame, whose footer and events border rich cut off with "...";
    # a proxy's error page or a traceback, a line of the header each, cut off the numbers too
    lines = live_frame(key_with(args, team, failure), width, height)
    out = "\n".join(lines)
    assert len(lines) <= height and "..." not in out
    assert "$10.500" in out and "Runs out in" in out and "Projected at reset" in out
    assert ("$7,241.00" in out) == (team == "team")
    assert ("team info unavailable" in out) == team.startswith("team info refused")
    if failure:  # the newest event, the same error, may have to give way to it
        assert FAILURES[failure][2] in out and "gateway not answering" in out and "backing off" in out
    else:
        assert "Events (newest first)" in out and "Ctrl+C to quit" in out


@pytest.mark.parametrize(("room", "foot", "events"), [(3, True, True), (2, False, True), (0, True, False), (-1, False, False)])
def test_footer_makes_way_for_the_newest_event_only(args, room, foot, events):
    # one event takes 3 lines: the footer goes to make room for it, but stays when it can't, if it fits on its own
    mon = many(args, 0, 0)
    _, keep_foot, rest = fit_rest(Console(width=80), mon, mon.summary(), args, None, Text("footer"), room)
    assert keep_foot == foot and bool(rest) == events


def test_limits_row_stays_when_no_event_fits_anyway(args):
    # the one-line limits row and footer fit in 2 lines, the newest event (3) doesn't: the row went for it all the same
    mon = many(args, 0, 0)
    assert fit_rest(Console(width=80), mon, mon.summary(), args, Text("limits"), Text("footer"), 0) == (True, True, [])


@pytest.mark.parametrize(("width", "height"), [(80, 24), (100, 30), (120, 30), (132, 43)])
def test_tall_event_is_cut_short_live(args, width, height):
    # a traceback made the newest event 44 lines tall: no event fitted, and the limits row, the tables and the budget
    # alerts went with it, leaving the screen half blank; live, an event is cut to one paragraph, as the header's error
    lines = live_frame(key_with(args, "team", "traceback"), width, height)
    out = "\n".join(lines)
    assert len(lines) <= height and "..." not in out
    if height > 24:  # on 80x24 the header and the numbers leave no room for an event
        assert "Events (newest first)" in out and "OK → HTTP 500: Internal Server Error, Traceback" in out
        assert "team budget 97% used" in out and "line 1419" not in out
    if height > 30:
        assert "RPM limit" in out and "Ctrl+C to quit" in out


def test_tall_first_event_shows_without_data(args):
    mon = polled(args, Snapshot(time.time(), *FAILURES["traceback"][:2], 2034.0))
    lines = live_frame(mon, 80, 24)
    out = "\n".join(lines)
    assert len(lines) <= 24 and "first poll: HTTP 500 — Internal Server Error" in out and "Ctrl+C to quit" in out
    assert "line 1419, in info_key_fn" in text_of(render(mon, Console(width=120), once=True))  # --once has it all


@pytest.mark.parametrize(("team", "failure"), [("team", "refused"), ("team", "traceback"), ("no team", "refused"), ("team", None)])
def test_model_limits_give_way_on_a_short_screen(args, team, failure):
    # a per-model budget is a row of the Budget panel: on 80x24 three of them pushed the end of the burn-rate panel off a
    # team key's offline screen; once nothing below the budget row is left to give way, they do, the last first
    mon = key_with(args, team, failure, limits=8)
    lines = live_frame(mon, 80, 24)
    out = "\n".join(lines)
    assert len(lines) <= 24 and "..." not in out
    assert "Runs out in" in out and "Projected at reset" in out and "Model limit $50.000" in out and "Model … and" in out
    tall = "\n".join(live_frame(mon, 80, 60))
    assert tall.count("Model limit $") == 8 and "Model … and" not in tall


def test_live_refresh_reuses_the_fit_until_something_changes(args, monkeypatch):
    calls = []
    monkeypatch.setattr("keymeter.tui.fit_rest", lambda *a: calls.append(a) or fit_rest(*a))
    mon = many(args, 30, 20)
    console, fitted = Console(file=io.StringIO(), width=120, height=40, record=True), {}
    for _ in range(3):
        render(mon, console, fitted=fitted)
    assert len(calls) == 1  # refreshes between polls fit nothing again
    mon.events.append((time.time(), "warn", "something happened"))
    render(mon, console, fitted=fitted)
    console.size = (100, 40)
    render(mon, console, fitted=fitted)
    assert len(calls) == 3
    console.print(render(mon, console, fitted=fitted))
    assert "something happened" in console.export_text()


@pytest.mark.parametrize(("snaps", "runout"), [
    ((ok(T0, spend=0.0, max_budget="inf"), ok(T0 + 600, spend=1.0, max_budget="inf")), "no cap"),  # no budget, as in LiteLLM
    ((ok(T0, spend=0.0, max_budget=1e9), ok(T0 + 600, spend=1e-300, max_budget=1e9)), "not at this rate"),  # in inf seconds
    ((ok(T0, spend="nan", max_budget=10), ok(T0 + 600, spend="nan", max_budget=10)), "—"),
    ((ok(T0, spend=1.0, rpm_limit=float("inf"), tpm_limit=float("nan")),), "no cap"),  # JSON's Infinity and NaN
], ids=["infinite budget", "tiny spend step", "spend not a number", "limits not finite"])
def test_numbers_that_are_not_finite_do_not_crash(args, snaps, runout):
    mon = polled(args, *snaps)
    for once in (True, False):
        console = Console(file=io.StringIO(), width=120, height=60, record=True)
        console.print(render(mon, console, once=once))
        assert runout in next(ln for ln in console.export_text().splitlines() if "Runs out in" in ln)


def test_control_characters_from_the_gateway_do_not_reach_the_terminal(args):
    evil = "x\x1b]0;pwned\x07\x1b[2J\x9b2J"  # set the window title, clear the screen (7- and 8-bit)
    snap = ok(time.time(), spend=1.0, max_budget=10, key_alias=evil, team_id=evil, rpm_limit=evil, model_spend={evil: 1.0})
    snap.team_keys = [{"key_alias": evil, "spend": 1.0}]
    mon = polled(args, snap, Snapshot(time.time(), "OFFLINE", evil))
    for once in (True, False):
        console = Console(file=io.StringIO(), width=200, height=60, record=True)
        console.print(render(mon, console, once=once))
        out = console.export_text()
        assert "\x1b" not in out and "\x9b" not in out and "]0;pwned" in out


@pytest.mark.parametrize(("models", "shown"), [(["a", None, 3], "a, None, 3"), (5, "5"), ("gpt-4o", "gpt-4o")])
def test_gateway_values_that_are_not_strings_do_not_crash(args, models, shown):
    snap = ok(time.time(), spend=1.0, max_budget=10, models=models)
    snap.team_keys = [{"key_alias": 12345, "spend": 1.0}]
    mon = polled(args, snap)
    for once in (True, False):
        console = Console(file=io.StringIO(), width=200, height=60, record=True)
        console.print(render(mon, console, once=once))
        out = console.export_text()
        assert "12345" in out and f"Allowed models  {shown} " in out


def test_negative_budget_is_used_up(args):
    # the status line calls it OVER BUDGET, whatever run-out time the monitor sends
    s = polled(args, ok(T0, spend=1.0, max_budget=-5), ok(T0 + 600, spend=2.0, max_budget=-5)).summary()
    s["key_info"]["runs_out_in_s"] = None
    line = next(ln for ln in text_of(render_burn(s, args, T0 + 600)).splitlines() if "Runs out in" in ln)
    assert "used up" in line
