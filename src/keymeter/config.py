"""Saved keys: the list `keymeter add` and the web dashboard's Keys dialog keep, and which one to watch.

They live in keys.json in the user's config folder (%APPDATA%\\keymeter on Windows, $XDG_CONFIG_HOME/keymeter or
~/.config/keymeter elsewhere), readable by the user only:
  {"active": "deepseek", "keys": [{"name": "deepseek", "gateway": "deepseek", "key": "sk-...", "url": null}]}
Every change reads the file again under a lock (keys.lock, next to it), so keymeter processes and the web
dashboard's request threads can't lose each other's changes. Entries this version can't use (written by a newer
one, or edited by hand) are left out of what it shows and kept in the file as they are.
"""
import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from keymeter import gateways
from keymeter.util import mask

MAX_NAME = 40
ENV_NAME = "KEYMETER_KEY"  # what the key from the environment goes by next to the saved ones; no saved key may take it
_threads = threading.Lock()

if os.name == "nt":
    import msvcrt

    def _lock_file(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # tries for about 10 seconds, then raises

    def _unlock_file(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock_file(f):
        fcntl.flock(f, fcntl.LOCK_UN)


class Error(ValueError):
    """A key that can't be saved, used or found; the message says why."""


def path():
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "keymeter" / "keys.json"


def label(gateway):
    """What the Keys dialog and `keymeter keys` call a gateway."""
    cls = gateways.GATEWAYS.get(gateway)
    return cls.label if cls else gateway


@contextlib.contextmanager
def _locked():
    """Hold the saved keys for one read, or one read and write, against this process's other threads and against
    other keymeter processes."""
    p = path()
    with _threads:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            f = open(p.with_name("keys.lock"), "a+b")  # noqa: SIM115 - closed below, after the unlock
        except OSError as e:
            raise Error(f"can't use the keys folder {p.parent}: {e}") from None
        with f:
            try:
                _lock_file(f)
            except OSError as e:
                raise Error(f"can't lock {f.name}: {e}") from None
            try:
                yield
            finally:
                _unlock_file(f)


def _read():
    """keys.json as it is; entries keymeter can't use are kept as they are."""
    p = path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": None, "keys": []}
    except (OSError, ValueError) as e:
        raise Error(f"can't read the saved keys in {p}: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.setdefault("keys", []), list):
        raise Error(f"can't read the saved keys in {p}: it isn't what keymeter writes")
    return data


def _replace(src, dst):
    for attempt in range(40):  # Windows refuses while another program (a virus scanner, an editor) has the file open
        try:
            return os.replace(src, dst)
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def _save(data):
    """Write keys.json through a temporary file of its own (so a crash keeps the old one), readable by the user only."""
    p = path()
    try:
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".keys-", suffix=".tmp")  # mode 0600 where modes count
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            _replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as e:
        raise Error(f"can't save the keys in {p}: {e}") from None


def usable(k):
    """Whether keymeter can watch the saved entry `k`."""
    return (isinstance(k, dict) and isinstance(k.get("name"), str) and isinstance(k.get("key"), str)
            and isinstance(k.get("gateway"), str) and k["gateway"] in gateways.GATEWAYS
            and (k.get("url") is None or isinstance(k["url"], str)))


def _view(data):
    keys = [k for k in data["keys"] if usable(k)]
    active = data.get("active")
    return {"active": active if find({"keys": keys}, active) else None, "keys": keys}


def load():
    """The saved keys keymeter can use -> {"active": name or None, "keys": [entry, ...]}."""
    if not path().exists():
        return {"active": None, "keys": []}
    with _locked():
        return _view(_read())


def find(store, name):
    return next((k for k in store["keys"] if isinstance(k, dict) and k.get("name") == name), None)


def choose(name=None, extra=None):
    """The key to watch: the saved key `name`, else `extra` (the key from the environment), else the saved key watched
    last, else the first saved one; None when there is none. The saved keys are read only when needed, so a damaged
    keys.json doesn't stop KEYMETER_KEY."""
    if name is None and extra is not None:
        return extra
    store = load()
    if name is not None:
        e = find(store, name)
        if e is None:
            raise Error(f"no saved key named {name!r}; `keymeter keys` lists them")
        return e
    return find(store, store["active"]) or (store["keys"][0] if store["keys"] else None)


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
    if name == ENV_NAME:
        return f"{ENV_NAME} is what the key from the environment goes by; pick another name"
    return f"a key named {name!r} is already saved" if find(store, name) else ""


def adapter(e, team=True):
    """The gateway adapter that watches entry `e`."""
    try:
        return gateways.create(e["gateway"], e.get("url"), e["key"], team=team)
    except ValueError as err:
        raise Error(str(err)) from None


def entry(gateway, key, url=None, name=None):
    """A key to watch, checked for what can be checked without asking the gateway."""
    key, url = (key or "").strip(), (url or "").strip() or None
    if not key:
        raise Error("the API key is empty")
    e = {"name": name, "gateway": gateway, "key": key, "url": url}
    e["gateway"] = adapter(e).name  # resolves auto; raises for an unknown gateway or a URL keymeter can't use
    return e


def new_entry(store, gateway, key, url=None, name=None):
    """entry() for a key to save, named `name` or after its gateway."""
    e = entry(gateway, key, url)
    e["name"] = (name or "").strip() or suggest_name(store, e["gateway"])
    problem = name_error(store, e["name"])
    if problem:
        raise Error(problem)
    return e


def check(e):
    """Poll the gateway once with the key -> "" when it answers with the key's data, else what it answered."""
    try:
        snap = adapter(e, team=False).fetch()
    except Exception as err:  # noqa: BLE001 - an answer keymeter can't read
        return f"{type(err).__name__}: {err}"
    return "" if snap.key else snap.status + (f": {snap.message}" if snap.message else "")


def add(e):
    """Save entry `e` and make it the key to watch."""
    with _locked():
        data = _read()
        problem = name_error(data, e["name"])
        if problem:
            raise Error(problem)
        data["keys"].append(e)
        data["active"] = e["name"]
        _save(data)


def select(name):
    with _locked():
        data = _read()
        if not find(_view(data), name):
            raise Error(f"no saved key named {name!r}; `keymeter keys` lists them")
        data["active"] = name
        _save(data)


def remove(name):
    """Forget a saved key (one keymeter can't use, too); the next one becomes the key to watch if it was."""
    with _locked():
        data = _read()
        kept = [k for k in data["keys"] if not (isinstance(k, dict) and k.get("name") == name)]
        if len(kept) == len(data["keys"]):
            raise Error(f"no saved key named {name!r}; `keymeter keys` lists them")
        data["keys"] = kept
        if data.get("active") == name:
            usable_keys = _view(data)["keys"]
            data["active"] = usable_keys[0]["name"] if usable_keys else None
        _save(data)


def public(e):
    """What the page and `keymeter keys` show of a key: its name, gateway, URL, and the key masked."""
    return {"name": e["name"], "gateway": e["gateway"], "label": label(e["gateway"]), "key": mask(e["key"]),
            "url": e.get("url")}
