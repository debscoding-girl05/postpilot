"""Auth primitives: password hashing + JWT session tokens."""
from app import auth


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse battery")
    assert h != "correct horse battery"  # hashed, not plaintext
    assert auth.verify_password(h, "correct horse battery") is True
    assert auth.verify_password(h, "wrong password") is False


def test_token_roundtrip():
    token = auth.create_token(42)
    assert isinstance(token, str) and token.count(".") == 2  # JWT shape
    assert auth.decode_token(token) == 42


def test_bad_token_returns_none():
    assert auth.decode_token("not-a-token") is None
    assert auth.decode_token("") is None


def test_token_signed_with_app_secret(monkeypatch):
    token = auth.create_token(7)
    # A token signed with a different secret must not validate.
    monkeypatch.setenv("APP_SECRET", "a-totally-different-secret")
    assert auth.decode_token(token) is None


def test_agent_token_format():
    t = auth.new_agent_token()
    assert t.startswith("ppa_") and len(t) > 20
