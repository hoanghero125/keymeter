"""Gateway adapters.

An adapter polls one gateway with the key and returns a Snapshot whose `key` dict uses LiteLLM's
/key/info field names, so the monitor and both dashboards treat every gateway the same way. Each
adapter has `name`, `default_url`, `url`, `key`, `fetch() -> Snapshot`, and `features`: the optional
parts of the dashboard it can fill ("models" = spend per model, "limits" = rate limits and allowed
models, "team" = team budget and keys).
"""
from urllib.parse import urlparse

from keymeter.gateways.base import Snapshot, base_url
from keymeter.gateways.litellm import LiteLLM
from keymeter.gateways.openrouter import OpenRouter

GATEWAYS = {"litellm": LiteLLM, "openrouter": OpenRouter}

__all__ = ["GATEWAYS", "LiteLLM", "OpenRouter", "Snapshot", "create", "detect"]


def openrouter_host(url):
    host = urlparse(url).hostname or ""
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def detect(url, key):
    """Pick a gateway from the URL's host, or from the key's prefix when no URL is set."""
    if url:
        return "openrouter" if openrouter_host(url) else "litellm"
    return "openrouter" if key.startswith("sk-or-") else "litellm"


def create(name, url, key, team=True):
    """Adapter for gateway `name` ("auto", "litellm" or "openrouter"); `url` falls back to its default."""
    if name == "auto":
        name = detect(url, key)
    if name not in GATEWAYS:
        raise ValueError(f"unknown gateway {name!r}: use auto, {' or '.join(GATEWAYS)}")
    url = base_url(url or GATEWAYS[name].default_url)
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ValueError(f"the gateway URL must start with http:// or https:// (got {url!r})")
    if name == "openrouter" and not urlparse(url).path and openrouter_host(url):
        # OpenRouter's API lives under /api: https://openrouter.ai -> https://openrouter.ai/api (a proxy's root may be the API root)
        url += "/api"
    return LiteLLM(url, key, team=team) if name == "litellm" else OpenRouter(url, key)
