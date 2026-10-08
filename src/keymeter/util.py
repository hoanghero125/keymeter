"""Small helpers shared by the monitor, the gateway adapters and both dashboards."""
import math
from datetime import datetime, timezone


def num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_time(v):
    """ISO timestamp from the gateway -> unix seconds (naive times are UTC)."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def usd(x):
    if x is None:
        return "—"
    if abs(x) >= 100:
        return f"${x:,.2f}"
    if abs(x) >= 1:
        return f"${x:.3f}"
    if x and abs(x) < 0.01:
        return f"${x:.5f}"
    return f"${x:.4f}"


def fmt_dur(sec):
    if sec is None:
        return "—"
    sign, sec = ("-" if sec < 0 else ""), int(abs(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{sign}{d}d {h}h"
    if h:
        return f"{sign}{h}h {m:02d}m"
    if m:
        return f"{sign}{m}m {s:02d}s"
    return f"{sign}{s}s"


def mask(key):
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "sk-…"


def tidy(obj, key=""):
    """Round floats for JSON output (spend to 6 decimals, durations to whole seconds); NaN and inf become None."""
    if isinstance(obj, dict):
        return {k: tidy(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [tidy(v) for v in obj]
    if not isinstance(obj, float):
        return obj
    if not math.isfinite(obj):  # JSON has no NaN or Infinity (the page can't parse them), and round() can't take them
        return None
    return round(obj) if key.endswith("_s") else round(obj, 6)
