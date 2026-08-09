"""Password hashers for hashes created by the legacy Python application."""

from __future__ import annotations

import base64
import hashlib

import bcrypt
from django.contrib.auth.hashers import BasePasswordHasher, constant_time_compare


class LegacyPbkdf2Sha256PasswordHasher(BasePasswordHasher):
    """Verify the legacy URL-safe PBKDF2 format and upgrade it on login.

    The legacy app used the same algorithm name as Django, but encoded both the
    salt and digest with URL-safe base64. Django's built-in PBKDF2 hasher uses
    its own encoded format, so the legacy value must have a distinct algorithm
    name while it is being verified.
    """

    algorithm = "legacy_pbkdf2_sha256"

    def encode(self, password, salt, iterations=None):
        iterations = iterations or 600_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
        encoded = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        return f"{self.algorithm}${iterations}${salt}${encoded(digest)}"

    def verify(self, password, encoded):
        try:
            algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
            if algorithm != self.algorithm:
                return False
            decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            expected = decode(digest_text)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), decode(salt_text), int(iterations))
            return constant_time_compare(actual, expected)
        except (TypeError, ValueError):
            return False

    def must_update(self, encoded):
        return True


class LegacyBcryptPasswordHasher(BasePasswordHasher):
    """Verify raw bcrypt values used by older deployments and upgrade them."""

    algorithm = "legacy_bcrypt"

    def encode(self, password, salt):
        return f"{self.algorithm}${bcrypt.hashpw(password.encode('utf-8'), salt.encode('ascii')).decode('ascii')}"

    def verify(self, password, encoded):
        try:
            algorithm, bcrypt_hash = encoded.split("$", 1)
            return algorithm == self.algorithm and bcrypt.checkpw(password.encode("utf-8"), bcrypt_hash.encode("ascii"))
        except (TypeError, ValueError):
            return False

    def must_update(self, encoded):
        return True
