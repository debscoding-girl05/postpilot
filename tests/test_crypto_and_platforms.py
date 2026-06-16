import importlib

import pytest

from app import crypto
from app.platforms import SUPPORTED_PLATFORMS, get_driver


def test_crypto_roundtrip():
    data = {"identifier": "me.bsky.social", "app_password": "abcd-efgh"}
    token = crypto.encrypt_json(data)
    assert token != "" and "app_password" not in token  # encrypted, not plaintext
    assert crypto.decrypt_json(token) == data


def test_crypto_empty_token_returns_empty_dict():
    assert crypto.decrypt_json(None) == {}
    assert crypto.decrypt_json("") == {}


def test_crypto_wrong_secret_fails_gracefully(monkeypatch):
    token = crypto.encrypt_json({"a": 1})
    monkeypatch.setenv("APP_SECRET", "a-different-secret-entirely")
    importlib.reload(crypto)
    try:
        # Wrong key -> InvalidToken caught -> empty dict, not an exception.
        assert crypto.decrypt_json(token) == {}
    finally:
        monkeypatch.setenv("APP_SECRET", "test-secret-fixed-value-for-determinism")
        importlib.reload(crypto)


@pytest.mark.parametrize("platform", SUPPORTED_PLATFORMS)
def test_get_driver_returns_matching_driver(platform):
    driver = get_driver(platform)
    assert driver.name == platform
    assert driver.char_limit > 0


def test_get_driver_unknown_raises():
    with pytest.raises(ValueError):
        get_driver("friendster")


def test_adapt_caption_default_truncation():
    driver = get_driver("twitter")  # char_limit 280
    assert driver.adapt_caption("hi") == "hi"
    long = "z" * 500
    out = driver.adapt_caption(long)
    assert len(out) == 280 and out.endswith("...")
