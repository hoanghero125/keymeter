"""Gateway adapters.

An adapter polls one gateway with the key and returns a Snapshot whose `key` dict uses LiteLLM's
/key/info field names, so the monitor and both dashboards treat every gateway the same way; a gateway with
a prepaid balance instead of a budget (DeepSeek) adds `balance` and `available`. Each adapter has `name`,
`default_url`, `url`, `key`, `fetch() -> Snapshot`, and `features`: the optional parts of the dashboard it can
fill ("models" = spend per model, "limits" = rate limits and allowed models, "team" = team budget and keys).
"""
from urllib.parse import urlparse

from keymeter.gateways.base import Snapshot, base_url
from keymeter.gateways.deepseek import DeepSeek
from keymeter.gateways.litellm import LiteLLM
from keymeter.gateways.openrouter import OpenRouter

GATEWAYS = {"litellm": LiteLLM, "openrouter": OpenRouter, "deepseek": DeepSeek}

__all__ = ["GATEWAYS", "DeepSeek", "LiteLLM", "OpenRouter", "Snapshot", "create", "detect"]


def on_host(url, domain):
    """Whether the URL's host is `domain` or a subdomain of it."""
    host = urlparse(url).hostname or ""
    return host == domain or host.endswith("." + domain)


def detect(url, key):
    """Pick a gateway from the URL's host, or from the key's prefix when no URL is set."""
    if url:
        return "openrouter" if on_host(url, "openrouter.ai") else "deepseek" if on_host(url, "deepseek.com") else "litellm"
    return "openrouter" if key.startswith("sk-or-") else "litellm"


def create(name, url, key, team=True):
    """Adapter for gateway `name` ("auto" or a key of GATEWAYS); `url` falls back to its default."""
    if name == "auto":
        name = detect(url, key)
    if name not in GATEWAYS:
        names = ["auto", *GATEWAYS]
        raise ValueError(f"unknown gateway {name!r}: use {', '.join(names[:-1])} or {names[-1]}")
    url = base_url(url or GATEWAYS[name].default_url)
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ValueError(f"the gateway URL must start with http:// or https:// (got {url!r})")
    if name == "openrouter" and not urlparse(url).path and on_host(url, "openrouter.ai"):
        # OpenRouter's API lives under /api: https://openrouter.ai -> https://openrouter.ai/api (a proxy's root may be the API root)
        url += "/api"
    return LiteLLM(url, key, team=team) if name == "litellm" else GATEWAYS[name](url, key)
