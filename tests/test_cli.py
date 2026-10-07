import json
import os

import pytest
from conftest import KEY

from keymeter.cli import load_env, main


def test_load_env_parses_lines_and_keeps_real_environment(clean_env, monkeypatch):
    env = clean_env / "settings.env"
    env.write_text('# comment\n\nKEYMETER_KEY="sk-from-file"\nKEYMETER_URL = \'http://gw:4000\'\nnot a setting\n'
                   "KEYMETER_GATEWAY=litellm\n", encoding="utf-8")
    monkeypatch.setenv("KEYMETER_GATEWAY", "openrouter")
    load_env(env, required=True)
    assert os.environ["KEYMETER_KEY"] == "sk-from-file"
    assert os.environ["KEYMETER_URL"] == "http://gw:4000"
    assert os.environ["KEYMETER_GATEWAY"] == "openrouter"


def test_missing_key_exits_with_help(clean_env):
    with pytest.raises(SystemExit, match="KEYMETER_KEY is not set"):
        main(["json"])


def test_missing_env_file_exits(clean_env):
    with pytest.raises(SystemExit, match="--env file not found"):
        main(["json", "--env", "nope.env"])


def test_unknown_gateway_from_environment_exits(clean_env, monkeypatch):
    monkeypatch.setenv("KEYMETER_KEY", KEY)
    monkeypatch.setenv("KEYMETER_GATEWAY", "openai")
    with pytest.raises(SystemExit, match="unknown gateway 'openai'"):
        main(["json"])


def test_json_reads_dotenv_in_working_directory(clean_env, fake_gateway, capsys):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 2.5, "max_budget": 10}})})
    (clean_env / ".env").write_text(f"KEYMETER_KEY={KEY}\nKEYMETER_URL={gw.url}\n", encoding="utf-8")
    assert main(["json"]) == 0
    out = capsys.readouterr().out
    s = json.loads(out)
    assert s["status"] == "OK" and s["gateway_type"] == "litellm"
    assert s["key_info"]["spend"] == 2.5 and s["key_info"]["used_pct"] == 25.0
    assert KEY not in out


def test_json_exits_1_when_not_ok(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/api/v1/key": (401, {"error": {"message": "No auth credentials found"}})})
    monkeypatch.setenv("KEYMETER_KEY", "sk-or-v1-0123456789abcdef")
    assert main(["json", "--url", gw.url + "/api", "--gateway", "openrouter"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "AUTH ERROR"


def test_json_skips_team_with_no_team(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/key/info": (200, {"info": {"spend": 1, "team_id": "t1"}})})
    monkeypatch.setenv("KEYMETER_KEY", KEY)
    main(["json", "--url", gw.url, "--no-team"])
    assert [p for p, _, _ in gw.requests] == ["/key/info"]


def test_tui_once_renders(clean_env, fake_gateway, monkeypatch, capsys):
    gw = fake_gateway({"/key/info": (200, {"info": {"key_alias": "dev", "spend": 2.5, "max_budget": 10,
                                                    "model_spend": {"gpt-x": 2.5}}})})
    monkeypatch.setenv("KEYMETER_KEY", KEY)
    assert main(["tui", "--once", "--url", gw.url]) == 0
    out = capsys.readouterr().out
    assert "keymeter · litellm" in out and "gpt-x" in out and "25.0%" in out


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])  # Notepad / Out-File, and PowerShell 5.1's `>`
def test_load_env_reads_windows_encodings(clean_env, encoding):
    env = clean_env / ".env"
    env.write_text("KEYMETER_KEY=sk-from-windows\n", encoding=encoding)
    load_env(env, required=True)
    assert os.environ["KEYMETER_KEY"] == "sk-from-windows"


def test_load_env_rejects_other_encodings(clean_env):
    env = clean_env / ".env"
    env.write_bytes(b"KEYMETER_KEY=\xff\xfe\xfa\n")
    with pytest.raises(SystemExit, match="save it as UTF-8"):
        load_env(env, required=True)
