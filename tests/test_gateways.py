import pytest

from keymeter import gateways
from keymeter.gateways import LiteLLM, OpenRouter


@pytest.mark.parametrize(("url", "key", "expected"), [
    (None, "sk-or-v1-abc", "openrouter"),
    (None, "sk-1234", "litellm"),
    ("https://openrouter.ai/api/v1", "sk-1234", "openrouter"),
    ("https://eu.openrouter.ai/api", "sk-1234", "openrouter"),
    ("https://llm.example.com", "sk-or-v1-abc", "litellm"),  # an explicit URL decides
    ("https://notopenrouter.ai", "sk-1234", "litellm"),
])
def test_detect(url, key, expected):
    assert gateways.detect(url, key) == expected


def test_create_uses_default_urls():
    lite = gateways.create("auto", None, "sk-1234")
    assert isinstance(lite, LiteLLM) and lite.url == "http://localhost:4000"
    router = gateways.create("auto", None, "sk-or-v1-abc")
    assert isinstance(router, OpenRouter) and router.url == "https://openrouter.ai/api"


@pytest.mark.parametrize("url", ["https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/", "https://openrouter.ai/api/"])
def test_create_accepts_openai_style_base_urls(url):
    assert gateways.create("openrouter", url, "sk-or-v1-abc").url == "https://openrouter.ai/api"


@pytest.mark.parametrize("url", ["https://openrouter.ai", "https://openrouter.ai/", "https://openrouter.ai/v1"])
def test_create_adds_api_to_a_bare_openrouter_url(url):
    assert gateways.create("auto", url, "sk-or-v1-abc").url == "https://openrouter.ai/api"


@pytest.mark.parametrize(("url", "expected"), [
    ("https://eu.openrouter.ai/", "https://eu.openrouter.ai/api"),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),  # a proxy whose root is OpenRouter's API root, as in 0.1.0
    ("https://llm.example.com/v1", "https://llm.example.com"),
])
def test_create_adds_api_only_on_openrouter_hosts(url, expected):
    assert gateways.create("openrouter", url, "sk-or-v1-abc").url == expected


def test_create_passes_team_flag_to_litellm():
    assert gateways.create("litellm", "http://gw", "sk-1234", team=False).team_allowed is False


def test_create_rejects_unknown_gateway():
    with pytest.raises(ValueError, match="unknown gateway 'openai'"):
        gateways.create("openai", None, "sk-1234")


@pytest.mark.parametrize("url", ["llm.example.com", "ftp://llm.example.com", "http://"])
def test_create_rejects_urls_without_http(url):
    with pytest.raises(ValueError, match="must start with http:// or https://"):
        gateways.create("litellm", url, "sk-1234")
