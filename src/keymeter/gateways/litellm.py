"""LiteLLM proxy: GET /key/info for the calling key, plus /team/info when the key is allowed to read it.

Both endpoints are read-only and cost no tokens.
"""
import time

from keymeter.gateways.base import Snapshot, error_snapshot, error_text, http_get, unwrap


class LiteLLM:
    name = "litellm"
    default_url = "http://localhost:4000"
    features = frozenset({"models", "limits", "team"})

    def __init__(self, url, key, team=True):
        self.url, self.key = url, key
        self.team_allowed = team
        self.team_denied = ""   # the 401/403 that stopped /team/info queries, reported again on every later poll
        self.key_param = False  # older LiteLLM versions need /key/info?key=...

    def fetch(self):
        params = {"key": self.key} if self.key_param else None
        status, body, ms = http_get(self.url, self.key, "/key/info", params)
        # the key goes into the URL (and so into access logs) only when LiteLLM asks for the parameter
        if not self.key_param and (status == 422 or (status == 200 and "spend" not in unwrap(body, "info"))):
            self.key_param = True
            status, body, ms = http_get(self.url, self.key, "/key/info", {"key": self.key})
        if status != 200 or not isinstance(body, dict):
            return error_snapshot(status, body, ms)

        snap = Snapshot(time.time(), "OK", latency_ms=ms, key=unwrap(body, "info"))
        team_id = snap.key.get("team_id")
        if team_id and self.team_allowed:
            ts, tbody, _ = http_get(self.url, self.key, "/team/info", {"team_id": team_id})
            if ts == 200 and isinstance(tbody, dict):
                snap.team = unwrap(tbody, "team_info")
                snap.team_keys = tbody.get("keys") or []
            else:
                snap.team_error = f"HTTP {ts}: {error_text(tbody)}"[:160]
                if ts in (401, 403):
                    self.team_allowed = False
                    self.team_denied = snap.team_error
                    snap.notices.append("this key can't read /team/info — showing key data only")
        elif team_id:
            snap.team_error = self.team_denied  # "" when the team is off (--no-team)
        return snap
