import pytest
from conftest import KEY

from keymeter import gateways
from keymeter.gateways.deepseek import DeepSeek


def balance(total, currency="USD", available=True):
    return {"is_available": available, "balance_infos": [
        {"currency": currency, "total_balance": total, "granted_balance": "0.00", "topped_up_balance": total}]}


def test_fetch_reads_the_usd_balance(fake_gateway):
    gw = fake_gateway({"/user/balance": (200, balance("8.42"))})
    snap = DeepSeek(gw.url, KEY).fetch()
    assert snap.status == "OK"
    assert snap.key == {"balance": 8.42, "available": True, "spend": 0.0}
    assert gw.requests == [("/user/balance", {}, f"Bearer {KEY}")]


def test_spend_is_what_the_balance_went_down_by(fake_gateway):
    totals = iter(["10.00", "9.50", "9.25", "20.00", "19.00"])  # a top-up to $20 adds no spend
    gw = fake_gateway({"/user/balance": lambda q: (200, balance(next(totals)))})
    ds = DeepSeek(gw.url, KEY)
    assert [ds.fetch().key["spend"] for _ in range(5)] == pytest.approx([0.0, 0.5, 0.75, 0.75, 1.75])


def test_failed_poll_keeps_the_last_balance(fake_gateway):
    answers = iter([(200, balance("10.00")), (502, {"error": {"message": "bad gateway"}}), (200, balance("9.00"))])
    gw = fake_gateway({"/user/balance": lambda q: next(answers)})
    ds = DeepSeek(gw.url, KEY)
    assert [ds.fetch().key.get("spend") for _ in range(3)] == [0.0, None, 1.0]


def test_picks_the_usd_balance_among_several_currencies(fake_gateway):
    body = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "110.00"},
                                                    {"currency": "USD", "total_balance": "3.10"}]}
    gw = fake_gateway({"/user/balance": (200, body)})
    assert DeepSeek(gw.url, KEY).fetch().key["balance"] == 3.1


def test_account_without_a_usd_balance_is_an_error(fake_gateway):
    gw = fake_gateway({"/user/balance": (200, balance("110.00", currency="CNY"))})
    snap = DeepSeek(gw.url, KEY).fetch()
    assert snap.status == "ERROR"
    assert snap.message == "keymeter reads USD balances only; this account's balance is in CNY"
    assert snap.key == {}


def test_account_with_no_balance_at_all_has_zero(fake_gateway):
    gw = fake_gateway({"/user/balance": (200, {"is_available": False, "balance_infos": []})})
    snap = DeepSeek(gw.url, KEY).fetch()
    assert snap.status == "OK"
    assert snap.key == {"balance": 0.0, "available": False, "spend": 0.0}


@pytest.mark.parametrize("total", ["n/a", None, "NaN", "Infinity"])
def test_balance_that_is_not_a_number_is_an_error(fake_gateway, total):
    gw = fake_gateway({"/user/balance": (200, balance(total))})
    snap = DeepSeek(gw.url, KEY).fetch()
    assert snap.status == "ERROR"
    assert snap.message == f"can't read the USD balance {total!r}"
    assert snap.key == {}


@pytest.mark.parametrize("body", [{"balance_infos": "USD 3.10"}, ["USD", "3.10"], {}])
def test_odd_answer_is_an_error(fake_gateway, body):
    gw = fake_gateway({"/user/balance": (200, body)})
    snap = DeepSeek(gw.url, KEY).fetch()
    assert snap.status == "HTTP 200"
    assert snap.key == {}


def test_fetch_auth_error(fake_gateway):
    body = {"error": {"message": "Authentication Fails, Your api key: ****1234 is invalid", "type": "authentication_error",
                      "param": None, "code": "invalid_request_error"}}
    gw = fake_gateway({"/user/balance": (401, body)})
    snap = DeepSeek(gw.url, KEY).fetch()
    assert snap.status == "AUTH ERROR"
    assert snap.message == "Authentication Fails, Your api key: ****1234 is invalid"


def test_openai_style_url_reaches_user_balance(fake_gateway):
    gw = fake_gateway({"/user/balance": (200, balance("1.00"))})
    assert gateways.create("deepseek", gw.url + "/v1", KEY).fetch().status == "OK"
    assert [p for p, _, _ in gw.requests] == ["/user/balance"]
