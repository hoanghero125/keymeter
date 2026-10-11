import json
import os

import pytest
from conftest import KEY

from keymeter import cli, config, gateways
from keymeter.cli import main


def answers(monkeypatch, key, *lines):
    """Type `key` at the hidden prompt and `lines` at the others; returns the prompts shown."""
    shown, typed = [], iter(lines)
    monkeypatch.setattr("getpass.getpass", lambda prompt: shown.append(prompt) or key)
    monkeypatch.setattr("builtins.input", lambda prompt: shown.append(prompt) or next(typed))
    return shown


def balance(total="8.42"):
    return {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": total}]}


def saved(name, gateway="deepseek", key=KEY, url=None):
    return {"name": name, "gateway": gateway, "key": key, "url": url}


# ---------------------------------------------------------------- the store

def test_keys_live_in_the_user_config_folder(clean_env):
    assert config.path() == clean_env / "config" / "keymeter" / "keys.json"
    assert config.load() == {"active": None, "keys": []}


def test_add_select_remove(clean_env):
    config.add(saved("a"))
    config.add(saved("b", "litellm", url="http://gw"))
    assert config.load()["active"] == "b"  # the key added last is watched
    config.select("a")
    assert config.load()["active"] == "a"
    config.remove("a")
    store = config.load()
    assert [k["name"] for k in store["keys"]] == ["b"] and store["active"] == "b"
    if os.name != "nt":
        assert config.path().stat().st_mode & 0o777 == 0o600


def test_unknown_names_and_taken_names_are_errors(clean_env):
    config.add(saved("a"))
    for call in (lambda: config.select("x"), lambda: config.remove("x")):
        with pytest.raises(config.Error, match="no saved key named 'x'"):
            call()
    with pytest.raises(config.Error, match="a key named 'a' is already saved"):
        config.add(saved("a"))


@pytest.mark.parametrize("name", ["", " a", "-a", "a\x1b[2J", "x" * 41])
def test_odd_names_are_refused(clean_env, name):
    assert config.name_error(config.load(), name)


def test_names_follow_the_gateway(clean_env):
    config.add(saved("deepseek"))
    assert config.new_entry(config.load(), "deepseek", KEY)["name"] == "deepseek-2"


def test_entry_checks_what_it_can_without_the_gateway():
    with pytest.raises(config.Error, match="the API key is empty"):
        config.entry("deepseek", "  ")
    with pytest.raises(config.Error, match="must start with http:// or https://"):
        config.entry("litellm", KEY, "llm.example.com")
    assert config.entry("auto", "sk-or-v1-abc")["gateway"] == "openrouter"


def test_a_damaged_file_is_an_error(clean_env):
    config.path().parent.mkdir(parents=True)
    config.path().write_text("{nope", encoding="utf-8")
    with pytest.raises(config.Error, match="can't read the saved keys"):
        config.load()


def test_odd_entries_in_the_file_are_left_out(clean_env):
    config.path().parent.mkdir(parents=True)
    config.path().write_text(json.dumps({"active": "gone", "keys": [saved("ok"), {"name": "x"}, saved("y", "openai"), 3]}),
                             encoding="utf-8")
    assert config.load() == {"active": None, "keys": [saved("ok")]}


def test_public_masks_the_key():
    assert config.public(saved("a")) == {"name": "a", "gateway": "deepseek", "label": "DeepSeek", "key": "sk-tes…1234",
                                         "url": None}


# ---------------------------------------------------------------- keymeter add, keys, use, remove

def test_add_asks_for_the_gateway_checks_the_key_and_saves_it(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/user/balance": (200, balance())})
    shown = answers(monkeypatch, KEY, "3", "")
    assert main(["add", "--url", gw.url]) == 0
    assert config.load() == {"active": "deepseek", "keys": [saved("deepseek", url=gw.url)]}
    assert shown == ["Gateway: ", "API key (typing is hidden): ", "Name [deepseek]: "]
    out = capsys.readouterr().out
    assert "The key works." in out and "keymeter web" in out and KEY not in out


def test_add_asks_for_the_url_of_a_litellm_proxy(clean_env, fake_gateway, monkeypatch):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 1.0}})})
    shown = answers(monkeypatch, KEY, "litellm", "llm.example.com", gw.url, "work")  # no http://: asked again
    assert main(["add"]) == 0
    assert config.load()["keys"] == [saved("work", "litellm", url=gw.url)]
    assert shown.count("Proxy URL [http://localhost:4000]: ") == 2


def test_add_asks_again_for_an_unknown_gateway_or_a_taken_name(clean_env, fake_gateway, monkeypatch):
    gw = fake_gateway({"/user/balance": (200, balance())})
    config.add(saved("deepseek"))
    shown = answers(monkeypatch, KEY, "7", "3", "deepseek", "")
    assert main(["add", "--url", gw.url]) == 0
    assert shown.count("Gateway: ") == 2 and shown.count("Name [deepseek-2]: ") == 2
    assert config.load()["active"] == "deepseek-2"


def test_add_skips_questions_the_options_answer(clean_env, fake_gateway, monkeypatch):
    gw = fake_gateway({"/user/balance": (200, balance())})
    shown = answers(monkeypatch, KEY)
    assert main(["add", "--gateway", "deepseek", "--url", gw.url, "--name", "ds"]) == 0
    assert shown == ["API key (typing is hidden): "] and config.load()["active"] == "ds"


@pytest.mark.parametrize(("save", "code"), [("", 1), ("y", 0)])
def test_add_saves_a_key_that_fails_only_when_told_to(clean_env, fake_gateway, monkeypatch, capsys, save, code):
    gw = fake_gateway({"/user/balance": (401, {"error": {"message": "Authentication Fails"}})})
    answers(monkeypatch, KEY, save, "")
    assert main(["add", "--gateway", "deepseek", "--url", gw.url]) == code
    assert "The gateway answered AUTH ERROR: Authentication Fails." in capsys.readouterr().out
    assert len(config.load()["keys"]) == (save == "y")


def test_ctrl_c_while_asking_saves_nothing(clean_env, monkeypatch):
    def interrupt(prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    with pytest.raises(SystemExit, match="cancelled"):
        main(["add"])
    assert not config.path().exists()


def test_keys_lists_the_saved_keys_masked(clean_env, capsys):
    config.add(saved("ds"))
    config.add(saved("work", "litellm", key="sk-work-0123456789", url="http://gw"))
    assert main(["keys"]) == 0
    out = capsys.readouterr().out
    assert "   1  ds    DeepSeek       sk-tes…1234" in out
    assert " * 2  work  LiteLLM proxy  sk-wor…6789  http://gw" in out
    assert KEY not in out


def test_use_and_remove(clean_env, capsys):
    config.add(saved("a"))
    config.add(saved("b"))
    assert main(["use", "a"]) == 0 and config.load()["active"] == "a"
    assert main(["remove", "a"]) == 0 and config.load() == {"active": "b", "keys": [saved("b")]}
    with pytest.raises(SystemExit, match="no saved key named 'a'"):
        main(["use", "a"])


# ---------------------------------------------------------------- which key web, tui and json watch

def test_the_saved_key_watched_last_is_used_from_any_directory(clean_env, fake_gateway, capsys):
    gw = fake_gateway({"/user/balance": (200, balance())})
    config.add(saved("ds", url=gw.url))
    assert main(["json"]) == 0
    assert json.loads(capsys.readouterr().out)["key_info"]["balance"] == 8.42


def test_name_picks_a_saved_key(clean_env, fake_gateway, capsys):
    gw = fake_gateway({"/user/balance": (200, balance("1.00"))})
    config.add(saved("ds", url=gw.url))
    config.add(saved("other", url="http://127.0.0.1:9"))
    assert main(["json", "ds"]) == 0
    assert json.loads(capsys.readouterr().out)["key_info"]["balance"] == 1.0
    with pytest.raises(SystemExit, match="no saved key named 'nope'"):
        main(["json", "nope"])


def test_the_key_from_the_environment_wins_over_the_saved_one(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 1.0}})})
    config.add(saved("ds", key="sk-saved-0123456789"))
    monkeypatch.setenv("KEYMETER_KEY", KEY)
    assert main(["json", "--url", gw.url]) == 0
    assert gw.requests[0][2] == f"Bearer {KEY}"


@pytest.mark.parametrize("argv", [["json", "--url", "http://127.0.0.1:9"], ["json", "ds", "--gateway", "litellm"],
                                  ["tui", "--once", "--url", "http://127.0.0.1:9"]])
def test_gateway_options_are_refused_for_a_saved_key(clean_env, argv):
    # they would silently do nothing in the web page or the tui menu, so a saved key keeps its own everywhere
    config.add(saved("ds", url="http://127.0.0.1:9"))
    with pytest.raises(SystemExit, match="a saved key keeps its own"):
        main(argv)


@pytest.mark.parametrize("argv", [["web", "--url", "http://127.0.0.1:9"], ["tui", "--gateway", "litellm"]])
def test_gateway_options_are_refused_for_web_and_the_tui_menu_without_an_environment_key(clean_env, monkeypatch, argv):
    monkeypatch.setattr(cli, "interactive", lambda: True)
    with pytest.raises(SystemExit, match="a saved key keeps its own"):
        main(argv)


def test_dotenv_without_a_key_cannot_redirect_the_saved_key(clean_env, fake_gateway, capsys):
    # a .env of some other program, or one planted in a cloned repo, is not read at all
    gw = fake_gateway({"/user/balance": (200, balance())})
    config.add(saved("ds", url=gw.url))
    (clean_env / ".env").write_text("KEYMETER_URL=http://attacker.invalid\nHTTPS_PROXY=http://attacker.invalid\n", encoding="utf-8")
    assert main(["json"]) == 0
    assert "HTTPS_PROXY" not in os.environ and "KEYMETER_URL" not in os.environ


def test_dotenv_with_a_key_wins_over_the_saved_key(clean_env, fake_gateway, capsys):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 1.0}})})
    config.add(saved("ds", key="sk-saved-0123456789"))
    (clean_env / ".env").write_text(f"KEYMETER_KEY={KEY}\nKEYMETER_URL={gw.url}\n", encoding="utf-8")
    assert main(["json"]) == 0
    assert gw.requests[0][2] == f"Bearer {KEY}"


def test_first_run_in_a_terminal_adds_a_key_then_runs(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/user/balance": (200, balance())})
    monkeypatch.setattr(cli, "interactive", lambda: True)
    answers(monkeypatch, KEY, "3", "")
    assert main(["json", "--url", gw.url]) == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("\n{") + 1:])["key_info"]["balance"] == 8.42
    assert config.load()["active"] == "deepseek"


def test_no_key_outside_a_terminal_says_how_to_add_one(clean_env):
    with pytest.raises(SystemExit, match="No key to watch yet. Add one with `keymeter add`"):
        main(["json"])


def test_tui_asks_which_key_and_goes_back_to_the_question_after_ctrl_c(clean_env, monkeypatch):
    config.add(saved("a", url="http://127.0.0.1:9"))
    config.add(saved("b", url="http://127.0.0.1:9"))
    monkeypatch.setattr(cli, "interactive", lambda: True)
    watched = []
    monkeypatch.setattr("keymeter.tui.run", lambda mon, args: watched.append((mon.gateway.key, args.switch)))
    shown = answers(monkeypatch, KEY, "1", "", "q")  # watch 1; Ctrl+C; Enter watches it again; quit
    assert main(["tui"]) == 0
    assert shown == ["Watch [2]: ", "Watch [1]: ", "Watch [1]: "]  # the default follows the key picked
    assert watched == [(KEY, True), (KEY, True)]
    assert config.load()["active"] == "a"


def test_dotenv_with_a_key_cannot_route_a_saved_key_through_its_proxy(clean_env, fake_gateway, monkeypatch, capsys):
    # a planted .env with a key of its own and a proxy: picking a saved key must not send it through that proxy
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    gw = fake_gateway({"/user/balance": (200, balance())})
    config.add(saved("ds", url=gw.url))
    (clean_env / ".env").write_text("KEYMETER_KEY=sk-attacker-0123456789\nHTTP_PROXY=http://attacker.invalid:8080\n",
                                    encoding="utf-8")
    assert main(["json", "ds"]) == 0
    assert "HTTP_PROXY" not in os.environ
    assert gw.requests[0][2] == f"Bearer {KEY}"


# ---------------------------------------------------------------- what the review turned up

@pytest.mark.parametrize("url", ["http://[", "http://[::1"])
def test_a_url_keymeter_cant_read_is_an_error_not_a_crash(clean_env, monkeypatch, url):
    with pytest.raises(config.Error, match="can't read the gateway URL"):
        config.entry("auto", KEY, url)
    monkeypatch.setenv("KEYMETER_KEY", KEY)
    monkeypatch.setenv("KEYMETER_URL", url)
    with pytest.raises(SystemExit, match="can't read the gateway URL"):
        main(["json"])


def test_a_damaged_keys_file_does_not_stop_the_key_from_the_environment(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 1.0}})})
    config.path().parent.mkdir(parents=True)
    config.path().write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("KEYMETER_KEY", KEY)
    assert main(["json", "--url", gw.url]) == 0
    with pytest.raises(SystemExit, match="can't read the saved keys"):
        main(["keys"])


def test_writers_in_parallel_threads_lose_nothing(clean_env):
    import threading
    threads = [threading.Thread(target=config.add, args=(saved(f"k{i}"),)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(k["name"] for k in config.load()["keys"]) == sorted(f"k{i}" for i in range(12))
    assert not list(config.path().parent.glob("*.tmp"))


def test_writers_in_parallel_processes_lose_nothing(clean_env):
    import subprocess
    import sys
    code = ("import sys\nfrom keymeter import config\n"
            "for i in range(8):\n    config.add({'name': f'{sys.argv[1]}-{i}', 'gateway': 'deepseek', 'key': 'sk-x', 'url': None})\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, f"p{n}"], env=os.environ.copy()) for n in range(3)]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0]
    assert len(config.load()["keys"]) == 24


def test_entries_keymeter_cant_use_are_kept_in_the_file(clean_env):
    future = {"name": "next", "gateway": "a-gateway-from-a-newer-keymeter", "key": "sk-x", "url": None, "extra": [1]}
    config.path().parent.mkdir(parents=True)
    config.path().write_text(json.dumps({"active": "a", "keys": [saved("a"), future], "version": 9}), encoding="utf-8")
    assert [k["name"] for k in config.load()["keys"]] == ["a"]
    config.add(saved("b"))
    config.select("a")
    data = json.loads(config.path().read_text(encoding="utf-8"))
    assert future in data["keys"] and data["version"] == 9
    config.remove("next")  # still forgettable
    assert future not in json.loads(config.path().read_text(encoding="utf-8"))["keys"]


@pytest.mark.parametrize("entry", [{"name": "a", "gateway": "deepseek", "key": "k", "url": 5},
                                   {"name": "a", "gateway": ["deepseek"], "key": "k"},
                                   {"name": ["a"], "gateway": "deepseek", "key": "k"}, "a", None])
def test_odd_entries_are_left_out_without_a_crash(clean_env, capsys, entry):
    config.path().parent.mkdir(parents=True)
    config.path().write_text(json.dumps({"active": ["a"], "keys": [entry]}), encoding="utf-8")
    assert config.load() == {"active": None, "keys": []}
    assert main(["keys"]) == 0


@pytest.mark.parametrize("data", [{"keys": 5}, [1, 2], "x"])
def test_a_keys_file_of_another_shape_is_an_error_and_left_alone(clean_env, data):
    config.path().parent.mkdir(parents=True)
    config.path().write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(config.Error, match="isn't what keymeter writes"):
        config.add(saved("a"))
    assert json.loads(config.path().read_text(encoding="utf-8")) == data


def test_a_file_that_cant_be_written_is_an_error(clean_env, monkeypatch, capsys):
    config.add(saved("a"))

    def refuse(src, dst):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(config.os, "replace", refuse)
    monkeypatch.setattr(config.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="can't save the keys"):
        main(["remove", "a"])
    assert [k["name"] for k in config.load()["keys"]] == ["a"]
    assert not list(config.path().parent.glob("*.tmp"))


def test_the_environment_keys_name_is_taken(clean_env):
    assert "KEYMETER_KEY is what the key from the environment goes by" in config.name_error(config.load(), "KEYMETER_KEY")


def test_name_leaves_the_environment_alone(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/user/balance": (200, balance())})
    config.add(saved("ds", url=gw.url))
    monkeypatch.setenv("KEYMETER_KEY", KEY)
    monkeypatch.setenv("KEYMETER_GATEWAY", "bogus")
    monkeypatch.setenv("KEYMETER_URL", "llm.local")
    assert main(["json", "ds"]) == 0


def test_a_dotenv_that_isnt_utf8_belongs_to_another_program(clean_env, fake_gateway, capsys):
    gw = fake_gateway({"/user/balance": (200, balance())})
    config.add(saved("ds", url=gw.url))
    (clean_env / ".env").write_bytes(b"NAME=\xff\xfe\xfa\n")
    assert main(["json"]) == 0


def test_tui_menu_logs_only_the_first_key_watched(clean_env, monkeypatch, tmp_path, capsys):
    config.add(saved("a", url="http://127.0.0.1:9"))
    config.add(saved("b", url="http://127.0.0.1:9"))
    monkeypatch.setattr(cli, "interactive", lambda: True)
    logs = []
    monkeypatch.setattr("keymeter.tui.run", lambda mon, args: logs.append(args.log))
    answers(monkeypatch, KEY, "1", "2", "1", "q")
    assert main(["tui", "--log", str(tmp_path / "a.csv")]) == 0
    assert logs == [str(tmp_path / "a.csv"), None, str(tmp_path / "a.csv")]
    assert "Not logging b: --log covers a, the first key watched." in capsys.readouterr().out


def test_a_new_gateway_needs_only_its_adapter(clean_env, monkeypatch, capsys):
    class Future:
        name, label, default_url, features = "future", "Future AI", "https://future.example", frozenset()

        def __init__(self, url, key):
            self.url, self.key = url, key
    monkeypatch.setitem(gateways.GATEWAYS, "future", Future)
    config.add(saved("f", "future"))
    assert main(["keys"]) == 0
    assert "Future AI" in capsys.readouterr().out
