import json
import os

import pytest
from conftest import KEY

from keymeter import cli, config
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
    assert config.public(saved("a")) == {"name": "a", "gateway": "deepseek", "key": "sk-tes…1234", "url": None}


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


def test_url_option_applies_to_a_saved_key(clean_env, fake_gateway, capsys):
    gw = fake_gateway({"/user/balance": (200, balance())})
    config.add(saved("ds", url="http://127.0.0.1:9"))
    assert main(["json", "--url", gw.url]) == 0


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
