"""Questions in the terminal: add a key (`keymeter add`, or a first run without one) and pick the key to watch
(`keymeter tui`)."""
import getpass

from keymeter import config
from keymeter.gateways import GATEWAYS, LiteLLM


def choose_gateway():
    names = list(GATEWAYS)
    print("Which gateway is the key for?")
    for i, name in enumerate(names, 1):
        print(f"  {i}  {GATEWAYS[name].label}")
    while True:
        answer = input("Gateway: ").strip().lower()
        for i, name in enumerate(names, 1):
            if answer in (str(i), name):
                return name
        print(f"  type a number from 1 to {len(names)}")


def add_key(gateway=None, url=None, name=None):
    """Ask for a key and whatever the options leave open, check it, save it -> the saved entry, or None."""
    print(f"Add a key to watch. keymeter saves it in {config.path()}, in your user folder.")
    gateway = gateway if gateway and gateway != "auto" else choose_gateway()
    key = ""
    while not key:
        key = getpass.getpass("API key (typing is hidden): ").strip()
    ask_url = url is None and gateway == "litellm"  # OpenRouter and DeepSeek have one address
    while True:
        if ask_url:
            url = input(f"Proxy URL [{LiteLLM.default_url}]: ").strip() or None
        try:
            e = config.entry(gateway, key, url)
            break
        except config.Error as err:
            if not ask_url:
                raise
            print(f"  {err}")

    print("Checking the key…")
    problem = config.check(e)
    if problem:
        print(f"The gateway answered {problem}.")
        if input("Save it anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("Nothing saved.")
            return None
    else:
        print("The key works.")
    default = config.suggest_name(config.load(), gateway)
    while True:
        e["name"] = name or input(f"Name [{default}]: ").strip() or default
        problem = config.name_error(config.load(), e["name"])
        if not problem:
            break
        if name:
            raise config.Error(problem)
        print(f"  {problem}")
    config.add(e)
    print(f"Saved as {e['name']!r}; keymeter watches it from now on.")
    return e


def list_keys(store):
    """One line per saved key, numbered; the one watched last is marked with *."""
    rows = [config.public(k) for k in store["keys"]]
    width, label_width = max(len(p["name"]) for p in rows), max(len(p["label"]) for p in rows)
    for i, p in enumerate(rows, 1):
        mark = "*" if p["name"] == store["active"] else " "
        print(f" {mark}{i:>2}  {p['name']:<{width}}  {p['label']:<{label_width}}  {p['key']}" + (f"  {p['url']}" if p["url"] else ""))


def pick_key():
    """Ask which saved key to watch, or add one -> the entry, or None to quit."""
    while True:
        store = config.load()
        if not store["keys"]:
            print("No saved keys yet.")
            return add_key()
        default = next((i for i, k in enumerate(store["keys"], 1) if k["name"] == store["active"]), 1)
        print("Saved keys:")
        list_keys(store)
        print("   a  add a key\n   q  quit")
        answer = input(f"Watch [{default}]: ").strip().lower() or str(default)
        if answer == "q":
            return None
        if answer == "a":
            e = add_key()
            if e:
                return e
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(store["keys"]):
            e = store["keys"][int(answer) - 1]
            config.select(e["name"])
            return e
        print("  type a number, a or q")
