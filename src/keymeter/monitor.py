"""The polling state machine behind both dashboards.

Monitor polls a gateway adapter, keeps the spend samples inside the burn-rate window, notices budget
resets, status changes and threshold crossings, and turns all of it into one summary dict:
  - spend vs. budget, remaining, % used, time until the budget resets
  - burn rate ($/h), when the budget runs out at that rate, projected spend at reset
  - spend per model and per team key (when the gateway reports them)
  - rate limits, key expiry, blocked state, and an event log
"""
import csv
import math
import re
import time
from collections import deque
from pathlib import Path

from keymeter.gateways.base import Snapshot
from keymeter.util import iso, mask, num, parse_time, tidy, usd

MAX_BACKOFF = 60      # max seconds between polls while the gateway keeps failing
SPARK_LEN = 40        # per-poll spend deltas kept for the terminal sparkline
EVENT_LEN = 8         # events kept in the event log


# ---------------------------------------------------------------- derived views

def limit_of(v):
    """A budget or per-model limit as a number, or None; NaN or inf (from an odd gateway or proxy) counts as none."""
    b = num(v)
    return b if b is not None and math.isfinite(b) else None


def budget_of(d):
    """max_budget as a number, or None; as in LiteLLM, a NaN or inf budget is no budget."""
    return limit_of(d.get("max_budget"))


def budget_view(d, rate, now, first_spend):
    """Spend/budget numbers for one scope (key or team) plus projections from the burn rate."""
    spend = num(d.get("spend")) or 0.0
    budget = budget_of(d)
    reset = parse_time(d.get("budget_reset_at"))
    v = {
        "spend": spend,
        "max_budget": budget,
        "soft_budget": num(d.get("soft_budget")),
        "remaining": None,
        "used_pct": None,
        "budget_duration": d.get("budget_duration"),
        "budget_reset_at": d.get("budget_reset_at"),
        "reset_in_s": reset - now if reset else None,
        "burn_per_hour": rate,
        "runs_out_in_s": None,
        "projected_at_reset": None,
        "session_spend": spend - first_spend if first_spend is not None else None,
    }
    if budget is not None and budget >= 0:
        # under the budget, at least $0.000001: tidy() rounds to 6 decimals, and 0 means used up
        v["remaining"] = 0.0 if spend >= budget else max(budget - spend, 1e-6)
        v["used_pct"] = spend / budget * 100 if budget else 100.0
        if v["remaining"] <= 0:
            v["runs_out_in_s"] = 0.0  # used up, whatever the rate (spend stops once the gateway refuses the key)
        elif rate:  # at least 1 s: tidy() rounds to whole seconds, and 0 means used up
            v["runs_out_in_s"] = max(v["remaining"] / rate * 3600, 1.0)
    elif budget is not None:
        v["runs_out_in_s"] = 0.0  # a negative budget: the gateway refuses spend over it, so it is used up from the start
    if rate is not None and reset and reset > now:
        v["projected_at_reset"] = spend + rate * (reset - now) / 3600
    return v


def limit_view(d):
    return {
        "rpm_limit": d.get("rpm_limit"),
        "tpm_limit": d.get("tpm_limit"),
        "max_parallel_requests": d.get("max_parallel_requests"),
        "blocked": bool(d.get("blocked")),
        "models": d.get("models") or [],
    }


def dict_field(d, name):
    """A field LiteLLM sends as a dict; anything else (from an odd gateway or proxy) counts as empty."""
    v = d.get(name)
    return v if isinstance(v, dict) else {}


def model_rows(k, first):
    """Per-model spend from model_spend, joined with per-model budgets (model_max_budget)."""
    spend = {m: num(v) or 0.0 for m, v in dict_field(k, "model_spend").items()}
    usage = dict_field(k, "model_max_budget_usage")
    limits = {}
    for m, v in dict_field(k, "model_max_budget").items():
        if isinstance(v, dict):
            limits[m] = (limit_of(v.get("budget_limit")), v.get("time_period"))
        else:
            limits[m] = (limit_of(v), None)
    for m, u in usage.items():  # a key on a budget tier has no model_max_budget of its own, but its usage names the limit
        if m not in limits and isinstance(u, dict):
            limits[m] = (limit_of(u.get("budget_limit")), u.get("time_period"))
    total = sum(spend.values())
    rows = []
    for m in sorted(set(spend) | set(limits), key=lambda m: -spend.get(m, 0.0)):
        sp = spend.get(m, 0.0)
        lim, period = limits.get(m, (None, None))
        # per-model limits apply to spend within their time period, which LiteLLM reports from 1.90 on; all-time
        # model_spend says nothing about a limit that resets, so without it the row shows the limit alone (and no alerts)
        u = usage.get(m)
        period_spend = num(u.get("current_spend")) if isinstance(u, dict) else None
        f0 = first.get(f"model:{m}")
        rows.append({
            "model": m,
            "spend": sp,
            "session_spend": sp - f0 if f0 is not None else None,
            "share_pct": sp / total * 100 if total else None,
            "limit": lim,
            "period": period,
            "period_spend": period_spend,
            "used_pct": period_spend / lim * 100 if lim and period_spend is not None else None,
        })
    return rows


def team_key_rows(keys, own_key):
    rows = []
    for k in keys if isinstance(keys, list) else []:  # LiteLLM sends a list of dicts; anything else counts as empty
        if not isinstance(k, dict):
            continue
        key_name = str(k.get("key_name") or "")
        spend, budget = num(k.get("spend")) or 0.0, budget_of(k)
        rows.append({
            "key": k.get("key_alias") or key_name or str(k.get("token") or "")[:10],
            "spend": spend,
            "max_budget": budget,
            "used_pct": spend / budget * 100 if budget else None,
            "blocked": bool(k.get("blocked")),
            "you": bool(key_name) and key_name.endswith(own_key[-4:]),
        })
    rows.sort(key=lambda r: -r["spend"])
    return rows


def derive_status(snap):
    """Status for a poll that returned key data (the key itself may still be unusable)."""
    k, t = snap.key, snap.team
    if k.get("blocked"):
        return "BLOCKED"
    if t.get("blocked"):
        return "TEAM BLOCKED"
    for scope, d in (("", k), ("TEAM ", t)):
        spend, budget = num(d.get("spend")) or 0.0, budget_of(d)
        # as in LiteLLM: a key is refused at its budget (spend >= max_budget), a team only once over it (spend > max_budget)
        if budget is not None and (spend > budget if scope else spend >= budget):
            return scope + "OVER BUDGET"
    exp = parse_time(k.get("expires"))
    if exp and exp <= snap.ts:
        return "EXPIRED"
    return "OK"


def csv_text(s):
    """Keep spreadsheet apps from running gateway text as a formula (CSV injection)."""
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def team_from_message(msg):
    m = re.search(r"Team=([^\s,]+)", msg or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- monitor state

class Monitor:
    def __init__(self, gateway, args):
        self.gateway, self.args = gateway, args
        self.window = args.window * 60
        self.snap = None              # latest poll
        self.last_data = None         # latest poll that returned key data
        self.samples = deque()        # (ts, key_spend, team_spend) inside the burn-rate window
        self.deltas = deque(maxlen=SPARK_LEN)  # key spend added per poll
        self.events = deque(maxlen=EVENT_LEN)
        self.first = {}               # spend at session start: "key", "team", "model:<name>"
        self.session_ts = None
        self.alert = {}               # budget scope -> highest threshold level announced
        self.polls = self.fails = self.fail_streak = 0
        self.polling = False
        self.next_poll_at = None
        self.bell = False
        self.log_error = None         # last --log write error, so it is reported once

    # -- polling

    def poll(self):
        self.polls += 1
        try:
            snap = self._fetch()
            self._ingest(snap)
        except Exception as e:  # noqa: BLE001 - an answer we can't make sense of must not stop the dashboard
            snap = Snapshot(time.time(), "ERROR", f"{type(e).__name__}: {e}")
            self._ingest(snap)
        self.snap = snap
        if self.args.log:
            try:
                self._log_csv(snap)
                self.log_error = None
            except OSError as e:
                if str(e) != self.log_error:
                    self.log_error = str(e)
                    self._event("warn", f"can't write to {self.args.log}: {e.strerror or e}")
        return snap

    def next_delay(self):
        if not self.fail_streak:
            return self.args.interval
        return min(self.args.interval * 2 ** min(self.fail_streak - 1, 16), max(MAX_BACKOFF, self.args.interval))

    def _fetch(self):
        snap = self.gateway.fetch()
        for text in snap.notices:
            self._event("info", text)
        if snap.key:
            snap.status = derive_status(snap)
            if snap.status != "OK":
                team = snap.status.startswith("TEAM ")
                snap.message = f"gateway reports the {'team' if team else 'key'} as {snap.status.removeprefix('TEAM ').lower()}"
        return snap

    def _ingest(self, snap):
        prev = self.snap.status if self.snap else None
        if prev is None:
            self._event("good" if snap.status == "OK" else "crit",
                        f"first poll: {snap.status}" + (f" — {snap.message}" if snap.message else ""))
        elif snap.status != prev:
            if snap.status == "OK":
                self._event("good", f"{prev} → OK, gateway is usable again", bell=True)
            else:
                self._event("crit", f"{prev} → {snap.status}" + (f": {snap.message}" if snap.message else ""), bell=True)

        if not snap.key:
            self.fails += 1
            self.fail_streak += 1
            return
        self.fail_streak = 0
        self.last_data = snap

        key_spend = num(snap.key.get("spend")) or 0.0
        team_spend = num(snap.team.get("spend"))
        if self.samples:
            _, last_key, last_team = self.samples[-1]
            if key_spend < last_key - 1e-9 or (team_spend is not None and last_team is not None
                                               and team_spend < last_team - 1e-9):
                self._event("info", "spend went down — budget period reset, restarting burn-rate tracking")
                self.samples.clear()
                self.deltas.clear()
                self.first.clear()
                self.alert.clear()
        if self.samples:
            self.deltas.append(max(key_spend - self.samples[-1][1], 0.0))
        self.samples.append((snap.ts, key_spend, team_spend))
        while len(self.samples) > 2 and self.samples[1][0] <= snap.ts - self.window:
            self.samples.popleft()

        fresh = "key" not in self.first
        if fresh:
            self.session_ts = snap.ts
        self.first.setdefault("key", key_spend)
        if team_spend is not None:
            self.first.setdefault("team", team_spend)
        for m, v in dict_field(snap.key, "model_spend").items():
            if f"model:{m}" not in self.first:
                if not fresh:
                    self._event("info", f"new model in use: {m}")
                self.first[f"model:{m}"] = num(v) or 0.0

        self._check_thresholds(snap)

    def _check_thresholds(self, snap):
        scopes = [("key", num(snap.key.get("spend")), budget_of(snap.key))]
        if snap.team:
            scopes.append(("team", num(snap.team.get("spend")), budget_of(snap.team)))
        for r in model_rows(snap.key, self.first):
            if r["limit"]:
                scopes.append((f"model {r['model']}", r["period_spend"], r["limit"]))
        for scope, spend, budget in scopes:
            if spend is None or not budget or budget <= 0:
                continue
            frac = spend / budget
            level = 2 if frac >= self.args.crit else 1 if frac >= self.args.warn else 0
            if level > self.alert.get(scope, 0):
                self._event("crit" if level == 2 else "warn",
                            f"{scope} budget {frac:.0%} used ({usd(spend)} of {usd(budget)})", bell=True)
            self.alert[scope] = level

    def _event(self, level, text, bell=False):
        self.events.append((time.time(), level, text))
        self.bell = self.bell or bell

    def _log_csv(self, snap):
        path = Path(self.args.log)
        new = not path.exists() or path.stat().st_size == 0
        rate = self.rate(1) if snap.key else None
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "status", "key_spend", "key_max_budget", "team_spend",
                            "team_max_budget", "key_burn_per_hour", "latency_ms", "message"])
            # gateway values go through num(), so only numbers reach the number columns
            w.writerow(tidy([iso(snap.ts), snap.status, num(snap.key.get("spend")), num(snap.key.get("max_budget")),
                             num(snap.team.get("spend")), num(snap.team.get("max_budget")),
                             rate, round(snap.latency_ms), csv_text(snap.message)]))

    # -- derived numbers

    def rate(self, idx):
        """$/hour over the burn-rate window (idx 1 = key, 2 = team); None until 2 samples exist."""
        pts = [(s[0], s[idx]) for s in self.samples if s[idx] is not None]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1:
            return None
        return max(pts[-1][1] - pts[0][1], 0.0) / (pts[-1][0] - pts[0][0]) * 3600

    def summary(self):
        now = time.time()
        snap, data, gw = self.snap, self.last_data, self.gateway
        out = {
            "time": iso(now),
            "gateway": gw.url,
            "gateway_type": gw.name,
            "features": sorted(gw.features),
            "key": mask(gw.key),
            "status": snap.status if snap else "STARTING",
            "message": snap.message if snap else "",
            "latency_ms": round(snap.latency_ms) if snap else None,
            "data_age_s": round(now - data.ts) if data else None,
            "session_s": round(now - self.session_ts) if self.session_ts else None,
        }
        if not data:
            out["team_hint"] = team_from_message(out["message"])
            return out
        k, t = data.key, data.team
        out["key_info"] = {
            "alias": k.get("key_alias"),
            "name": k.get("key_name"),
            "team_id": k.get("team_id"),
            "user_id": k.get("user_id"),
            "expires": k.get("expires"),
            "expires_in_s": (parse_time(k.get("expires")) - now) if parse_time(k.get("expires")) else None,
            **budget_view(k, self.rate(1), now, self.first.get("key")),
            **limit_view(k),
        }
        if t:
            out["team_info"] = {
                "alias": t.get("team_alias"),
                "id": t.get("team_id"),
                **budget_view(t, self.rate(2), now, self.first.get("team")),
                **limit_view(t),
            }
        if data.team_error:
            out["team_error"] = data.team_error
        out["models"] = model_rows(k, self.first)
        out["team_keys"] = team_key_rows(data.team_keys, gw.key)
        out["recent_poll_spend"] = list(self.deltas)
        out["events"] = [{"time": iso(ts), "level": lvl, "text": txt} for ts, lvl, txt in self.events]
        return out
