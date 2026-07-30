from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

import bcrypt


PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${encode(salt)}${encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(encoded, str):
        return False
    if is_bcrypt_hash(encoded):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), encoded.encode("ascii"))
        except (TypeError, ValueError):
            return False
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        expected = decode(digest_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), decode(salt_text), int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def is_bcrypt_hash(encoded: str) -> bool:
    """Return true for password hashes produced by the reference Node app."""
    return isinstance(encoded, str) and encoded.startswith(("$2a$", "$2b$", "$2y$"))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_email_verification_token() -> str:
    return secrets.token_urlsafe(32)


def new_password_reset_token() -> str:
    return secrets.token_urlsafe(32)


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_admin_token(password: str, max_age_seconds: int = 8 * 60 * 60) -> str:
    issued_at = str(int(time.time()))
    signature = hmac.new(
        password.encode("utf-8"), issued_at.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{issued_at}.{signature}"


def verify_admin_token(token: str | None, password: str, max_age_seconds: int = 8 * 60 * 60) -> bool:
    if not token or not password or "." not in token:
        return False
    issued_at, signature = token.split(".", 1)
    try:
        age = int(time.time()) - int(issued_at)
    except ValueError:
        return False
    if age < 0 or age > max_age_seconds:
        return False
    expected = hmac.new(
        password.encode("utf-8"), issued_at.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)
