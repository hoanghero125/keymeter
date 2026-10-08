"""Terminal dashboard (`keymeter tui`), drawn with rich."""
import math
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from keymeter.util import fmt_dur, usd

SPARK_CHARS = "▁▂▃▄▅▆▇█"
STATUS_STYLE = {"OK": "bold white on green", "OFFLINE": "bold black on yellow",
                "RATE LIMITED": "bold black on yellow", "STARTING": "bold white on blue"}
EVENT_STYLE = {"good": "green", "info": "cyan", "warn": "yellow", "crit": "bold red"}
CONTROL = dict.fromkeys(c for c in (*range(32), *range(127, 160)) if chr(c) not in "\t\n")  # str.translate drops these


def clean(v):
    """Gateway text (and the dicts and lists around it) without control characters: rich passes ESC through, so a
    model name or an error message could otherwise clear the screen or retitle the window."""
    if isinstance(v, str):
        return v.translate(CONTROL)
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    return [clean(x) for x in v] if isinstance(v, list) else v


def flat(text):
    """Gateway text with its line breaks and runs of blanks as single spaces (a proxy's HTML error page, a traceback)."""
    return " ".join(text.split())


def clock(ts):
    """Weekday and time, or "" when the platform can't convert `ts` (Windows stops at the year 3000)."""
    try:
        return time.strftime("%a %H:%M", time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return ""


def pct_style(pct, args):
    if pct is None:
        return "cyan"
    if pct >= args.crit * 100:
        return "red"
    return "yellow" if pct >= args.warn * 100 else "green"


def sparkline(values):
    if not values:
        return ""
    values = [v if math.isfinite(v) else 0.0 for v in values]  # inf or nan from the gateway can't be scaled
    top = max(values)
    if top <= 0:
        return SPARK_CHARS[0] * len(values)
    return "".join(SPARK_CHARS[min(int(v / top * (len(SPARK_CHARS) - 1) + 0.5), len(SPARK_CHARS) - 1)]
                   for v in values)


def fmt_limit(v):
    # gateway text goes in a Text, so rich doesn't read it as markup; so do inf and nan, which int() can't take
    number = isinstance(v, int) or isinstance(v, float) and math.isfinite(v)
    return "none" if v in (None, "") else f"{int(v):,}" if number else Text(str(v))


def render_header(mon, s, now, once=False):
    st = s["status"]
    top = Text()
    top.append(f" {st} ", style=STATUS_STYLE.get(st, "bold white on red"))
    top.append(f"  {s['gateway']}", style="bold")
    top.append(f"   key {s['key']}", style="dim")
    ki = s.get("key_info", {})
    if ki.get("alias"):
        top.append(f" ({ki['alias']})", style="dim")
    team = (s.get("team_info") or {}).get("alias") or ki.get("team_id") or s.get("team_hint")
    if team:
        top.append(f"   team {team}", style="bold cyan")

    info = Text(style="dim")
    if mon.polling:
        info.append("polling…")
    elif mon.snap:
        info.append(f"polled {fmt_dur(now - mon.snap.ts)} ago · {mon.snap.latency_ms:.0f} ms")
        if mon.next_poll_at:
            info.append(f" · next in {fmt_dur(max(mon.next_poll_at - now, 0))}")
    info.append(f" · {mon.polls} polls, {mon.fails} failed")
    if mon.fail_streak > 1:
        info.append(f" · backing off to {fmt_dur(mon.next_delay())}", style="yellow")

    lines = [top, info]
    if s.get("team_error"):  # there on every poll once /team/info refuses the key, so live it keeps to one line
        lines.append(Text(f"team info unavailable: {flat(s['team_error'])}", style="dim", no_wrap=not once, overflow="ellipsis"))
    if st != "OK" and s["message"]:  # cut short, so it leaves the numbers on the screen; --once has all of it
        msg = Text(flat(s["message"]), style="red")
        msg.truncate(200, overflow="ellipsis")  # three or four lines at 80 columns
        lines.append(msg)
    if mon.snap and mon.last_data and mon.snap is not mon.last_data:
        lines.append(Text(f"gateway not answering — numbers below are from {fmt_dur(s['data_age_s'])} ago",
                          style="yellow"))
    return Panel(Group(*lines), title=f"keymeter · {s['gateway_type']}", title_align="left",
                 border_style="green" if st == "OK" else "red")


def bar(pct, style, spend, budget, width=24):
    filled = 0 if math.isnan(pct) else round(min(max(pct, 0), 100) / 100 * width)  # nan: a spend that isn't a number
    t = Text("━" * filled, style=style)
    t.append("─" * (width - filled), style="bright_black")
    t.append(f"  {usd(spend)} / {usd(budget)}  ")
    t.append(f"{pct:.1f}%", style=f"bold {style}")
    return t


def render_budget(s, args, limit=None):
    """Key and team budgets, then the per-model limits; `limit` caps those (the rest are counted)."""
    g = Table.grid(padding=(0, 1))
    g.add_column(style="bold", no_wrap=True)
    g.add_column(overflow="fold")
    for label, v in (("Key", s["key_info"]), ("Team", s.get("team_info"))):
        if not v:
            continue
        style = pct_style(v["used_pct"], args)
        if v["used_pct"] is not None:
            g.add_row(label, bar(v["used_pct"], style, v["spend"], v["max_budget"]))
        elif v["max_budget"] is not None:  # a budget no % can be worked out from (a negative one)
            g.add_row(label, Text(f"{usd(v['spend'])} spent · budget {usd(v['max_budget'])}"))
        else:
            g.add_row(label, Text(f"{usd(v['spend'])} spent · no budget cap", style="dim"))
        detail = Text()
        if v["remaining"] is not None:
            detail.append(f"{usd(v['remaining'])} left", style=style)
        if v["reset_in_s"] is not None:  # an overdue reset shows as 0s, like the web UI
            detail.append(f"  resets in {fmt_dur(max(v['reset_in_s'], 0))}", style="dim")
        if v["budget_duration"]:
            detail.append(f" (every {v['budget_duration']})", style="dim")
        if v["soft_budget"]:
            detail.append(f"  soft limit {usd(v['soft_budget'])}", style="dim")
        if detail.plain:
            g.add_row("", detail)
    rows = [r for r in s["models"] if r["limit"]]
    for r in rows[:limit]:
        line = (bar(r["used_pct"], pct_style(r["used_pct"], args), r["period_spend"], r["limit"])
                if r["used_pct"] is not None else Text(f"limit {usd(r['limit'])}"))
        line.append(f"  {r['model']}" + (f" (per {r['period']})" if r["period"] else ""), style="dim")
        g.add_row("Model", line)
    if limit is not None and len(rows) > limit:
        g.add_row("Model", Text(f"… and {len(rows) - limit} more" if limit else f"{len(rows)} hidden", style="dim"))
    return Panel(g, title="Budget", title_align="left")


def render_burn(s, args, now):
    scopes = [("Key", s["key_info"])] + ([("Team", s["team_info"])] if s.get("team_info") else [])
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bold", no_wrap=True)
    for _ in scopes:  # values wrap rather than lose their end to "…" when the panel is narrow
        g.add_column(justify="right", overflow="fold")
    g.add_row("", *[Text(name, style="dim") for name, _ in scopes])

    def cell_rate(v):
        r = v["burn_per_hour"]
        return Text("measuring…", style="dim") if r is None else Text(f"{usd(r)}/h")

    def cell_session(v):
        if v["session_spend"] is None:
            return Text("—")
        return Text(f"+{usd(v['session_spend'])} in {fmt_dur(s['session_s'])}")

    def cell_runout(v):
        if v["max_budget"] is None:
            return Text("no cap", style="dim")
        # a negative budget has no remaining, but the gateway (and the status line) count any spend as over it
        if v["runs_out_in_s"] == 0 or v["max_budget"] < 0 or (v["remaining"] is not None and v["remaining"] <= 0):
            return Text("used up", style="red")
        if v["burn_per_hour"] is None:
            return Text("measuring…", style="dim")
        eta = v["runs_out_in_s"]
        if eta is None or eta == math.inf:  # inf: a burn too small to dent the budget
            return Text("not at this rate", style="green")
        if math.isnan(eta):  # from a spend or budget that isn't a number
            return Text("—", style="dim")
        if v["reset_in_s"] is not None and 0 < v["reset_in_s"] < eta:  # an overdue reset may still come too late
            return Text(f"{fmt_dur(eta)} (after reset)", style="green")
        style = "red" if eta < 3600 else "yellow"
        at = clock(now + eta) if eta < 7 * 86400 else ""  # a weekday is ambiguous further out
        return Text(fmt_dur(eta) + (f" · {at}" if at else ""), style=style)

    def cell_projected(v):
        p = v["projected_at_reset"]
        if p is None:
            return Text("—", style="dim")
        over = v["max_budget"] is not None and p > v["max_budget"]
        return Text(usd(p) + (" > budget" if over else ""), style="red" if over else "green")

    g.add_row(f"Burn rate ({args.window:g}m)", *[cell_rate(v) for _, v in scopes])
    g.add_row("This session", *[cell_session(v) for _, v in scopes])
    g.add_row("Runs out in", *[cell_runout(v) for _, v in scopes])
    g.add_row("Projected at reset", *[cell_projected(v) for _, v in scopes])
    return Panel(g, title="Burn rate", title_align="left")


def render_limits(s):
    ki, ti = s["key_info"], s.get("team_info")
    limits = "limits" in s["features"]
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bold", no_wrap=True)
    g.add_column(justify="right", overflow="fold")
    if ti:
        g.add_column(justify="right", overflow="fold")
        g.add_row("", Text("Key", style="dim"), Text("Team", style="dim"))
    if limits:
        for label, field_ in (("RPM limit", "rpm_limit"), ("TPM limit", "tpm_limit"),
                              ("Parallel requests", "max_parallel_requests")):
            g.add_row(label, fmt_limit(ki[field_]), *([fmt_limit(ti[field_])] if ti else []))
    exp = ki["expires_in_s"]
    exp_text = Text("never") if exp is None else Text(
        f"in {fmt_dur(exp)}" if exp > 0 else "expired", style="red" if exp is not None and exp < 86400 else "")
    g.add_row("Key expires", exp_text, *([""] if ti else []))
    if not limits:
        return Panel(g, title="Limits", title_align="left")
    models = ki["models"] or (ti["models"] if ti else [])
    models = models if isinstance(models, list) else [models]  # an odd gateway may send one name, or a number
    allowed = Text("Allowed models  ", style="bold")
    allowed.append(", ".join(map(str, models)) if models else "all")  # a null or a number in the list must not crash
    return Panel(Group(g, allowed), title="Limits", title_align="left")


def render_activity(s):
    d = s["recent_poll_spend"]
    body = Text()
    if d:
        body.append(sparkline(d), style="cyan")
        body.append(f"\nlast {len(d)} polls: +{usd(sum(d))}  ·  max/poll {usd(max(d))}  ·  "
                    f"active polls {sum(1 for x in d if x > 0)}/{len(d)}", style="dim")
    else:
        body.append("waiting for a second poll…", style="dim")
    return Panel(body, title="Key spend per poll", title_align="left")


def more_row(t, rows, limit):
    if limit is not None and len(rows) > limit:
        t.add_row(Text(f"… and {len(rows) - limit} more", style="dim"))


def render_models(s, limit=None):
    """Spend per model, top spenders first; `limit` caps the rows (the rest are counted)."""
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("Model", overflow="fold")  # the name gives way; numbers stay whole, neither cut with "…" nor split
    t.add_column("Spend", justify="right", no_wrap=True)
    t.add_column("This session", justify="right", no_wrap=True)
    t.add_column("Share", justify="right", no_wrap=True)
    for r in s["models"][:limit]:
        sess = r["session_spend"]
        t.add_row(Text(r["model"]), usd(r["spend"]),
                  Text(f"+{usd(sess)}", style="cyan" if sess else "dim") if sess is not None else "—",
                  f"{r['share_pct']:.0f}%" if r["share_pct"] is not None else "—")
    more_row(t, s["models"], limit)
    return Panel(t, title="Spend by model (key)", title_align="left")


def render_team_keys(s, args, limit=None):
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("Team key", overflow="fold")  # as in render_models
    t.add_column("Spend", justify="right", no_wrap=True)
    t.add_column("Budget", justify="right", no_wrap=True)
    t.add_column("Used", justify="right", no_wrap=True)
    for r in s["team_keys"][:limit]:
        name = Text(str(r["key"]), style="bold" if r["you"] else "")  # an alias can come as a number
        if r["you"]:
            name.append("  ← you", style="cyan")
        if r["blocked"]:
            name.append("  blocked", style="red")
        t.add_row(name, usd(r["spend"]), usd(r["max_budget"]),
                  Text(f"{r['used_pct']:.0f}%", style=pct_style(r["used_pct"], args))
                  if r["used_pct"] is not None else "—")
    more_row(t, s["team_keys"], limit)
    return Panel(t, title="Team keys", title_align="left")


def render_events(mon, limit=None, short=False):
    """The newest `limit` events; `short` (live), each cut to one paragraph as the header cuts the error, so a traceback
    or a proxy's error page leaves the rest of the dashboard on the screen."""
    body = Text()
    for i, (ts, level, text) in enumerate(list(reversed(mon.events))[:limit]):
        if i:
            body.append("\n")
        body.append(time.strftime("%H:%M:%S  ", time.localtime(ts)), style="dim")
        line = Text(flat(clean(text)) if short else clean(text), style=EVENT_STYLE.get(level, ""))
        if short:
            line.truncate(200, overflow="ellipsis")
        body.append(line)
    return Panel(body or Text("no events yet", style="dim"), title="Events (newest first)", title_align="left")


def side_by_side(width, *panels):
    if width < 110:
        return Group(*panels)
    g = Table.grid(expand=True)
    for _ in panels:
        g.add_column(ratio=1)
    g.add_row(*panels)
    return g


def render_tables(s, args, width, limit=None):
    tables = []
    if s.get("models"):
        tables.append(render_models(s, limit))
    if s.get("team_keys"):
        tables.append(render_team_keys(s, args, limit))
    return [side_by_side(width, *tables)] if tables else []


def height(console, renderable):
    return len(console.render_lines(renderable, pad=False))


def fit_tables(console, s, args, room, hidden=()):
    """The model and team-key tables with as many rows (top spenders first) as fit in `room` lines, or else a note of
    what is hidden: the tables, after the panels named in `hidden` (which keep the tables hidden too)."""
    n = max(len(s.get("models", [])), len(s.get("team_keys", [])))
    lo, hi = 0, 0 if hidden else min(n, room)  # a row takes a line at least
    mid = hi
    while lo < hi:  # binary search for the most rows that fit (0: not even one), trying them all first
        if height(console, Group(*render_tables(s, args, console.width, mid))) <= room:
            lo = mid
        else:
            hi = mid - 1
        mid = (lo + hi + 1) // 2
    if lo:
        return render_tables(s, args, console.width, lo)
    names = [*hidden, *(name for name, k in (("spend by model", "models"), ("team keys", "team_keys")) if s.get(k))]
    if not names:
        return []
    what = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    note = Text(f"{what} hidden: make the window taller to see them", style="dim")
    return [note] if height(console, note) <= room else []  # on a narrow screen the note takes two lines


def fit_rest(console, mon, s, args, row, foot, room):
    """What goes below the budget row in `room` lines (counted with the limits row `row`, if any, and the footer `foot`).
    Each comes in, in this order, if it fits in what is left: the newest event, the footer, the limits row (the tables
    go with it), the older events (newest first), then the rows of the tables (top spenders first) or a note of what is
    hidden. Returns (keep the limits row?, keep the footer?, [the tables or a note, the events])."""
    first, foot_h = height(console, render_events(mon, 1, short=True)), height(console, foot)
    row_h = 0 if row is None else height(console, row)
    room += foot_h + row_h
    shown = first <= room
    room -= first if shown else 0
    keep_foot = foot_h <= room
    room -= foot_h if keep_foot else 0
    keep_row = row is None or row_h <= room
    room -= row_h if keep_row else 0
    rest = []
    if shown:  # and the older events that fit
        events, n = render_events(mon, short=True), len(mon.events)
        while n > 1 and height(console, events) > room + first:
            n -= 1
            events = render_events(mon, n, short=True)
        room, rest = room + first - height(console, events), [events]
    return keep_row, keep_foot, [*fit_tables(console, s, args, room, [] if keep_row else ["limits", "spend per poll"]), *rest]


def render(mon, console, once=False, fitted=None):
    """The dashboard. Live, it is fitted to the screen (rich would cut off the bottom, events and all; see fit_rest).
    `fitted`, a dict kept between refreshes, holds on to the fit until a poll, a resize or a panel above changes height."""
    args, now, width = mon.args, time.time(), console.width
    s = clean(mon.summary())
    parts = [render_header(mon, s, now, once)]
    if "key_info" not in s:
        wait = "first poll in progress…" if not mon.snap else f"No quota data — the gateway answered {s['status']}."
        if mon.snap and not once:
            wait += f" Retrying (next in {fmt_dur(mon.next_delay())}); the dashboard fills in as soon as it answers."
        parts.append(Panel(Text(wait, style="yellow"), title="Quota", title_align="left"))
    else:
        burn, shown = render_burn(s, args, now), sum(1 for r in s["models"] if r["limit"])
        budget = side_by_side(width, render_budget(s, args), burn)
        while not once and shown and height(console, Group(parts[0], budget)) > console.height:
            shown -= 1  # live, the per-model limits give way, the last first, when the header and this row overflow
            budget = side_by_side(width, render_budget(s, args, shown), burn)
        parts.append(budget)
        parts.append(side_by_side(width, render_limits(s), render_activity(s)))
    if once:
        return Group(*parts, *render_tables(s, args, width), render_events(mon))
    foot = f"Ctrl+C to quit · poll every {args.interval:g}s · alerts at {args.warn:.0%} / {args.crit:.0%}"
    if args.log:
        foot += f" · logging to {args.log}"
    foot = Text(foot, style="dim")
    room = console.height - sum(height(console, p) for p in (*parts, foot))
    fitted = {} if fitted is None else fitted
    key = (mon.polls, tuple(mon.events), width, room)  # the tables and events only change with a poll
    if fitted.get("key") != key:
        fitted.update(key=key, fit=fit_rest(console, mon, s, args, parts[2] if len(parts) > 2 else None, foot, room))
    keep, keep_foot, rest = fitted["fit"]
    return Group(*(parts if keep else parts[:2]), *rest, *([foot] if keep_foot else []))


def run_live(mon, console, bell):
    fitted = {}  # the screen fit, reused while the data and the screen stay the same
    with Live(render(mon, console, fitted=fitted), console=console, auto_refresh=False) as live:
        while True:
            mon.polling = True
            live.update(render(mon, console, fitted=fitted), refresh=True)
            mon.poll()
            mon.polling = False
            if mon.bell and bell:
                live.console.bell()
            mon.bell = False
            mon.next_poll_at = time.time() + mon.next_delay()
            while True:
                live.update(render(mon, console, fitted=fitted), refresh=True)
                if time.time() >= mon.next_poll_at:
                    break
                time.sleep(0.5)


def run(mon, args):
    console = Console()
    if args.once:
        mon.poll()
        console.print(render(mon, console, once=True))
        return 0 if mon.snap.status == "OK" else 1
    try:
        run_live(mon, console, bell=not args.no_bell)
    except KeyboardInterrupt:
        pass
    ki = mon.summary().get("key_info")
    msg = f"Stopped after {mon.polls} polls ({mon.fails} failed)."
    if ki and ki["session_spend"] is not None:
        msg += f" Key spend this session: +{usd(ki['session_spend'])}, now {usd(ki['spend'])}"
        msg += f" of {usd(ki['max_budget'])}." if ki["max_budget"] else "."
    console.print(msg)
    return 0
