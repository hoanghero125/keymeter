"""Saved keys: the list `keymeter add` and the web dashboard's Keys dialog keep, and which one to watch.

They live in keys.json in the user's config folder (%APPDATA%\\keymeter on Windows, $XDG_CONFIG_HOME/keymeter or
~/.config/keymeter elsewhere), readable by the user only:
  {"active": "deepseek", "keys": [{"name": "deepseek", "gateway": "deepseek", "key": "sk-...", "url": null}]}
"""
import json
import os
from pathlib import Path

from keymeter import gateways
from keymeter.util import mask

LABELS = {"litellm": "LiteLLM proxy", "openrouter": "OpenRouter", "deepseek": "DeepSeek"}
MAX_NAME = 40


class Error(ValueError):
    """A key that can't be saved, used or found; the message says why."""


def path():
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "keymeter" / "keys.json"


def load():
    """The saved keys -> {"active": name or None, "keys": [entry, ...]}, empty when nothing is saved yet."""
    p = path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": None, "keys": []}
    except (OSError, ValueError) as e:
        raise Error(f"can't read the saved keys in {p}: {e}") from None
    data = data if isinstance(data, dict) else {}
    keys = [k for k in data.get("keys") or [] if isinstance(k, dict) and isinstance(k.get("name"), str)
            and isinstance(k.get("key"), str) and k.get("gateway") in gateways.GATEWAYS]
    store = {"active": None, "keys": keys}
    store["active"] = data.get("active") if find(store, data.get("active")) else None
    return store


def save(store):
    """Write the store to path(), readable by the user only; through a temporary file, so a crash keeps the old one."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    if os.name != "nt":
        os.chmod(tmp, 0o600)  # os.open keeps the mode of a file left over from a crash
    os.replace(tmp, p)


def find(store, name):
    return next((k for k in store["keys"] if k["name"] == name), None)


def suggest_name(store, gateway):
    """The gateway's name, or name-2, name-3, ... when that one is taken."""
    name, n = gateway, 1
    while find(store, name):
        n += 1
        name = f"{gateway}-{n}"
    return name


def name_error(store, name):
    """Why `name` can't name a new key ("" if it can)."""
    if not name or len(name) > MAX_NAME or not name.isprintable() or name.startswith("-") or name != name.strip():
        return f"a key name is 1 to {MAX_NAME} printable characters, not starting with - or a blank"
    return f"a key named {name!r} is already saved" if find(store, name) else ""


def entry(gateway, key, url=None, name=None):
    """A key to watch, checked for what can be checked without asking the gateway."""
    key, url = (key or "").strip(), (url or "").strip() or None
    if not key:
        raise Error("the API key is empty")
    if gateway == "auto":
        gateway = gateways.detect(url, key)
    if gateway not in gateways.GATEWAYS:
        names = ["auto", *gateways.GATEWAYS]
        raise Error(f"unknown gateway {gateway!r}: use {', '.join(names[:-1])} or {names[-1]}")
    e = {"name": name, "gateway": gateway, "key": key, "url": url}
    adapter(e)  # raises for a URL without http:// or https://
    return e


def new_entry(store, gateway, key, url=None, name=None):
    """entry() for a key to save, named `name` or after its gateway."""
    e = entry(gateway, key, url)
    e["name"] = (name or "").strip() or suggest_name(store, e["gateway"])
    problem = name_error(store, e["name"])
    if problem:
        raise Error(problem)
    return e


def adapter(e, team=True):
    """The gateway adapter that watches entry `e`."""
    try:
        return gateways.create(e["gateway"], e.get("url"), e["key"], team=team)
    except ValueError as err:
        raise Error(str(err)) from None


def check(e):
    """Poll the gateway once with the key -> "" when it answers with the key's data, else what it answered."""
    try:
        snap = adapter(e, team=False).fetch()
    except Exception as err:  # noqa: BLE001 - an answer keymeter can't read
        return f"{type(err).__name__}: {err}"
    return "" if snap.key else snap.status + (f": {snap.message}" if snap.message else "")


def add(e):
    """Save entry `e` and make it the key to watch. Each change reads the file again, so a key another keymeter
    process saved meanwhile stays."""
    store = load()
    problem = name_error(store, e["name"])
    if problem:
        raise Error(problem)
    store["keys"].append(e)
    store["active"] = e["name"]
    save(store)


def select(name):
    store = load()
    if not find(store, name):
        raise Error(f"no saved key named {name!r}; `keymeter keys` lists them")
    store["active"] = name
    save(store)


def remove(name):
    """Forget a saved key; the next one becomes the key to watch if it was."""
    store = load()
    e = find(store, name)
    if not e:
        raise Error(f"no saved key named {name!r}; `keymeter keys` lists them")
    store["keys"].remove(e)
    if store["active"] == name:
        store["active"] = store["keys"][0]["name"] if store["keys"] else None
    save(store)


def public(e):
    """What the page and `keymeter keys` show of a key: its name, gateway, URL, and the key masked."""
    return {"name": e["name"], "gateway": e["gateway"], "key": mask(e["key"]), "url": e.get("url")}
