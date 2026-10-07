"""Terminal dashboard (`keymeter tui`), drawn with rich."""
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


def clock(ts):
    return time.strftime("%a %H:%M", time.localtime(ts))


def pct_style(pct, args):
    if pct is None:
        return "cyan"
    if pct >= args.crit * 100:
        return "red"
    return "yellow" if pct >= args.warn * 100 else "green"


def sparkline(values):
    if not values:
        return ""
    top = max(values)
    if top <= 0:
        return SPARK_CHARS[0] * len(values)
    return "".join(SPARK_CHARS[min(int(v / top * (len(SPARK_CHARS) - 1) + 0.5), len(SPARK_CHARS) - 1)]
                   for v in values)


def fmt_limit(v):
    return "none" if v in (None, "") else f"{int(v):,}" if isinstance(v, (int, float)) else str(v)


def render_header(mon, s, now):
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
    if st != "OK" and s["message"]:
        lines.append(Text(s["message"], style="red"))
    if mon.snap and mon.last_data and mon.snap is not mon.last_data:
        lines.append(Text(f"gateway not answering — numbers below are from {fmt_dur(s['data_age_s'])} ago",
                          style="yellow"))
    return Panel(Group(*lines), title=f"keymeter · {s['gateway_type']}", title_align="left",
                 border_style="green" if st == "OK" else "red")


def bar(pct, style, spend, budget, width=24):
    filled = round(min(max(pct, 0), 100) / 100 * width)
    t = Text("━" * filled, style=style)
    t.append("─" * (width - filled), style="bright_black")
    t.append(f"  {usd(spend)} / {usd(budget)}  ")
    t.append(f"{pct:.1f}%", style=f"bold {style}")
    return t


def render_budget(s, args):
    g = Table.grid(padding=(0, 1))
    g.add_column(style="bold", no_wrap=True)
    g.add_column()
    for label, v in (("Key", s["key_info"]), ("Team", s.get("team_info"))):
        if not v:
            continue
        style = pct_style(v["used_pct"], args)
        if v["max_budget"] is not None:
            g.add_row(label, bar(v["used_pct"], style, v["spend"], v["max_budget"]))
        else:
            g.add_row(label, Text(f"{usd(v['spend'])} spent · no budget cap", style="dim"))
        detail = Text()
        if v["remaining"] is not None:
            detail.append(f"{usd(v['remaining'])} left", style=style)
        if v["reset_in_s"] is not None:
            detail.append(f"  resets in {fmt_dur(v['reset_in_s'])}", style="dim")
        if v["budget_duration"]:
            detail.append(f" (every {v['budget_duration']})", style="dim")
        if v["soft_budget"]:
            detail.append(f"  soft limit {usd(v['soft_budget'])}", style="dim")
        if detail.plain:
            g.add_row("", detail)
    for r in s["models"]:
        if r["limit"]:
            line = bar(r["used_pct"], pct_style(r["used_pct"], args), r["period_spend"], r["limit"])
            line.append(f"  {r['model']}" + (f" (per {r['period']})" if r["period"] else ""), style="dim")
            g.add_row("Model", line)
    return Panel(g, title="Budget", title_align="left")


def render_burn(s, args, now):
    scopes = [("Key", s["key_info"])] + ([("Team", s["team_info"])] if s.get("team_info") else [])
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bold", no_wrap=True)
    for _ in scopes:
        g.add_column(justify="right", no_wrap=True)
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
        if v["burn_per_hour"] is None:
            return Text("measuring…", style="dim")
        if v["runs_out_in_s"] is None:
            return Text("not at this rate", style="green")
        eta = v["runs_out_in_s"]
        if not eta:
            return Text("used up", style="red")
        if v["reset_in_s"] is not None and eta > v["reset_in_s"]:
            return Text(f"{fmt_dur(eta)} (after reset)", style="green")
        style = "red" if eta < 3600 else "yellow"
        return Text(f"{fmt_dur(eta)} · {clock(now + eta)}", style=style)

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
    g.add_column(justify="right", no_wrap=True)
    if ti:
        g.add_column(justify="right", no_wrap=True)
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
    allowed = Text("Allowed models  ", style="bold")
    allowed.append(", ".join(models) if models else "all")
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


def render_models(s):
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("Model")
    t.add_column("Spend", justify="right")
    t.add_column("This session", justify="right")
    t.add_column("Share", justify="right")
    for r in s["models"]:
        sess = r["session_spend"]
        t.add_row(r["model"], usd(r["spend"]),
                  Text(f"+{usd(sess)}", style="cyan" if sess else "dim") if sess is not None else "—",
                  f"{r['share_pct']:.0f}%" if r["share_pct"] is not None else "—")
    return Panel(t, title="Spend by model (key)", title_align="left")


def render_team_keys(s, args):
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("Team key")
    t.add_column("Spend", justify="right")
    t.add_column("Budget", justify="right")
    t.add_column("Used", justify="right")
    for r in s["team_keys"]:
        name = Text(r["key"], style="bold" if r["you"] else "")
        if r["you"]:
            name.append("  ← you", style="cyan")
        if r["blocked"]:
            name.append("  blocked", style="red")
        t.add_row(name, usd(r["spend"]), usd(r["max_budget"]),
                  Text(f"{r['used_pct']:.0f}%", style=pct_style(r["used_pct"], args))
                  if r["used_pct"] is not None else "—")
    return Panel(t, title="Team keys", title_align="left")


def render_events(mon):
    body = Text()
    for i, (ts, level, text) in enumerate(reversed(mon.events)):
        if i:
            body.append("\n")
        body.append(time.strftime("%H:%M:%S  ", time.localtime(ts)), style="dim")
        body.append(text, style=EVENT_STYLE.get(level, ""))
    return Panel(body or Text("no events yet", style="dim"), title="Events (newest first)", title_align="left")


def side_by_side(width, *panels):
    if width < 110:
        return Group(*panels)
    g = Table.grid(expand=True)
    for _ in panels:
        g.add_column(ratio=1)
    g.add_row(*panels)
    return g


def render(mon, width, once=False):
    args, now = mon.args, time.time()
    s = mon.summary()
    parts = [render_header(mon, s, now)]
    if "key_info" not in s:
        wait = "first poll in progress…" if not mon.snap else f"No quota data — the gateway answered {s['status']}."
        if mon.snap and not once:
            wait += f" Retrying (next in {fmt_dur(mon.next_delay())}); the dashboard fills in as soon as it answers."
        parts.append(Panel(Text(wait, style="yellow"), title="Quota", title_align="left"))
    else:
        parts.append(side_by_side(width, render_budget(s, args), render_burn(s, args, now)))
        parts.append(side_by_side(width, render_limits(s), render_activity(s)))
        tables = []
        if s["models"]:
            tables.append(render_models(s))
        if s["team_keys"]:
            tables.append(render_team_keys(s, args))
        if tables:
            parts.append(side_by_side(width, *tables))
    parts.append(render_events(mon))
    if not once:
        foot = f"Ctrl+C to quit · poll every {args.interval:g}s · alerts at {args.warn:.0%} / {args.crit:.0%}"
        if args.log:
            foot += f" · logging to {args.log}"
        parts.append(Text(foot, style="dim"))
    return Group(*parts)


def run_live(mon, console, bell):
    with Live(render(mon, console.width), console=console, auto_refresh=False) as live:
        while True:
            mon.polling = True
            live.update(render(mon, console.width), refresh=True)
            mon.poll()
            mon.polling = False
            if mon.bell and bell:
                live.console.bell()
            mon.bell = False
            mon.next_poll_at = time.time() + mon.next_delay()
            while True:
                live.update(render(mon, console.width), refresh=True)
                if time.time() >= mon.next_poll_at:
                    break
                time.sleep(0.5)


def run(mon, args):
    console = Console()
    if args.once:
        mon.poll()
        console.print(render(mon, console.width, once=True))
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
