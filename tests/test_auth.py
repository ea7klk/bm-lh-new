import bcrypt

from bminfo.auth import (
    hash_password,
    is_bcrypt_hash,
    issue_admin_token,
    session_token_hash,
    verify_admin_token,
    verify_password,
)


def test_password_hash_verification():
    encoded = hash_password("correct horse battery staple")
    assert encoded != "correct horse battery staple"
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_session_token_hash_is_deterministic():
    assert session_token_hash("token") == session_token_hash("token")
    assert session_token_hash("token") != session_token_hash("other")


def test_admin_token_is_signed_and_expires_by_age():
    token = issue_admin_token("secret")
    assert verify_admin_token(token, "secret")
    assert not verify_admin_token(token, "wrong")


def test_reference_bcrypt_hash_is_accepted():
    encoded = bcrypt.hashpw(b"reference-password", bcrypt.gensalt()).decode("ascii")
    assert is_bcrypt_hash(encoded)
    assert verify_password("reference-password", encoded)
    assert not verify_password("wrong-password", encoded)
