"""OpenRouter: GET /api/v1/key, translated into the LiteLLM field names the monitor works with.

OpenRouter reports the key's credit limit, what is left of it and how often it resets (daily, weekly
or monthly, on UTC boundaries). It has no per-model spend, rate limits or teams.
"""
import math
import time
from datetime import datetime, timedelta, timezone

from keymeter.gateways.base import Snapshot, error_snapshot, http_get
from keymeter.util import num

PERIODS = {"daily": "day", "weekly": "week", "monthly": "month"}


def next_reset(period, now):
    """Start of the next UTC day, week (Monday) or month after `now`, or None if the limit never resets."""
    d = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return d + timedelta(days=1)
    if period == "weekly":
        return d + timedelta(days=7 - d.weekday())
    if period == "monthly":
        return d.replace(year=d.year + d.month // 12, month=d.month % 12 + 1, day=1)
    return None


def translate(d, now):
    """/api/v1/key "data" -> the key fields the monitor reads."""
    limit, remaining = num(d.get("limit")), num(d.get("limit_remaining"))
    if limit is not None and not math.isfinite(limit):
        limit = None  # as in the monitor, a NaN or inf limit (from an odd proxy) is no limit
    reset = next_reset(d.get("limit_reset"), now) if limit is not None else None
    label = d.get("label") or ""
    return {
        # the default label is just the masked key, which the header already shows
        "key_alias": None if label.startswith("sk-or-") else label or None,
        # spend that counts against the limit (this period's when the limit resets)
        "spend": max(limit - remaining, 0.0) if limit is not None and remaining is not None else num(d.get("usage")) or 0.0,
        "max_budget": limit,
        "budget_duration": PERIODS.get(d.get("limit_reset")) if limit is not None else None,
        "budget_reset_at": reset.isoformat() if reset else None,
        "expires": d.get("expires_at"),
    }


class OpenRouter:
    name = "openrouter"
    default_url = "https://openrouter.ai/api"
    features = frozenset()

    def __init__(self, url, key):
        self.url, self.key = url, key

    def fetch(self):
        status, body, ms = http_get(self.url, self.key, "/v1/key")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            return error_snapshot(status, body, ms)
        now = time.time()
        return Snapshot(now, "OK", latency_ms=ms, key=translate(body["data"], now))
