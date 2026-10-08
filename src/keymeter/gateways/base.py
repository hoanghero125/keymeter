"""What every gateway adapter shares: the snapshot it returns and a small read-only HTTP client."""
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from keymeter import __version__

USER_AGENT = f"keymeter/{__version__}"


@dataclass
class Snapshot:
    """One poll. `key` and `team` use LiteLLM's /key/info and /team/info field names; other adapters translate into them."""
    ts: float
    status: str
    message: str = ""
    latency_ms: float = 0.0
    key: dict = field(default_factory=dict)        # spend, max_budget, budget_reset_at, expires, ...
    team: dict = field(default_factory=dict)
    team_keys: list = field(default_factory=list)
    team_error: str = ""
    notices: list = field(default_factory=list)    # info lines for the monitor's event log


def base_url(url):
    """Trim a trailing slash and a trailing /v1, so an OpenAI-style base URL works too."""
    return url.rstrip("/").removesuffix("/v1")


def http_get(base, key, path, params=None, timeout=15):
    """GET a gateway endpoint -> (status, body, latency_ms); status 0 means a network error."""
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        return 0, str(getattr(e, "reason", e)), (time.monotonic() - t0) * 1000
    latency = (time.monotonic() - t0) * 1000
    try:
        return status, json.loads(raw), latency
    except ValueError:
        return status, raw.decode("utf-8", "replace").strip()[:300], latency


def unwrap(body, inner):
    """LiteLLM wraps some responses ({"info": {...}}, {"team_info": {...}}); accept both."""
    if isinstance(body, dict) and isinstance(body.get(inner), dict):
        return body[inner]
    return body if isinstance(body, dict) else {}


def error_text(body):
    if isinstance(body, dict):
        err = body.get("error", body.get("detail", body))
        if isinstance(err, dict):
            return str(err.get("message", err))
        return str(err)
    return str(body)


def classify(status, msg):
    m = msg.lower()
    if status == 0:
        return "OFFLINE"
    # LiteLLM checks the key's team on every route, /key/info included, and names what it refused first:
    # "Team=t1 is blocked", "Budget has been exceeded! Team=t1 ..." (but "User=u1 in Team=t1" is a member's own budget)
    scope = "TEAM " if re.match(r"[^=]*\bteam=", m) else ""
    if "blocked" in m:
        return scope + "BLOCKED"
    if "budget" in m:
        return scope + "OVER BUDGET"
    if "expired" in m:
        return "EXPIRED"
    if status == 429:
        return "RATE LIMITED"
    if status in (401, 403):
        return "AUTH ERROR"
    return f"HTTP {status}"


def error_snapshot(status, body, latency_ms):
    """Snapshot for a poll that returned no key data."""
    msg = error_text(body) if status else f"network error: {body}"
    return Snapshot(time.time(), classify(status, msg), msg, latency_ms)
