"""DeepSeek: GET /user/balance, translated into the field names the monitor works with.

DeepSeek reports only the account's prepaid balance, the same for every key on the account: no budget, reset,
expiry, per-model spend or rate limits. The key gets `balance` (what is left), `available` (whether DeepSeek still
serves the account) and a `spend` that is what the balance went down by since keymeter started; a top-up raises
the balance and adds nothing. keymeter shows dollars only, so it reads the USD balance.
"""
import math
import time

from keymeter.gateways.base import Snapshot, error_snapshot, http_get
from keymeter.util import num


class DeepSeek:
    name = "deepseek"
    label = "DeepSeek"
    default_url = "https://api.deepseek.com"
    features = frozenset()

    def __init__(self, url, key):
        self.url, self.key = url, key
        self.last = None   # the balance at the last poll that returned one
        self.spent = 0.0   # what the balance went down by since the first

    def fetch(self):
        status, body, ms = http_get(self.url, self.key, "/user/balance")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("balance_infos"), list):
            return error_snapshot(status, body, ms)
        now, infos = time.time(), [b for b in body["balance_infos"] if isinstance(b, dict)]
        usd = next((b for b in infos if b.get("currency") == "USD"), None)
        if usd is None and infos:
            currencies = ", ".join(sorted({str(b.get("currency")) for b in infos}))
            return Snapshot(now, "ERROR", f"keymeter reads USD balances only; this account's balance is in {currencies}", ms)
        balance = num(usd.get("total_balance")) if usd else 0.0  # no balance in any currency: nothing left
        if balance is None or not math.isfinite(balance):
            return Snapshot(now, "ERROR", f"can't read the USD balance {usd.get('total_balance')!r}", ms)
        if self.last is not None and balance < self.last:
            self.spent += self.last - balance
        self.last = balance
        key = {"balance": balance, "available": body.get("is_available") is not False, "spend": self.spent}
        return Snapshot(now, "OK", latency_ms=ms, key=key)
