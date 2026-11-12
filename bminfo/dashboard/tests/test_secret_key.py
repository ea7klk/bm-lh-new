"""The signing key must fail closed, not fall back to something guessable.

Production ran for two months with `SECRET_KEY` set to the nine-character
administrator password, because settings read `APP_SECRET_KEY or
ADMIN_PASSWORD` and the deployment's `APP_SECRET_KEY` was present but empty.
Nothing failed; cookies were simply signed with a human password.

These tests pin the replacement rule, so the fallback cannot come back as a
well-meaning convenience.
"""

import os
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config import settings

LONG_KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def resolve(**env):
    """Call the resolver with a clean APP_SECRET_KEY/ADMIN_PASSWORD environment."""
    cleared = {"APP_SECRET_KEY": "", "ADMIN_PASSWORD": ""}
    cleared.update(env)
    with mock.patch.dict(os.environ, cleared, clear=False):
        return settings.resolve_secret_key()


class SecretKeyResolutionTests(SimpleTestCase):
    def test_a_long_key_is_used_verbatim(self):
        with mock.patch.object(settings, "DEBUG", False):
            self.assertEqual(resolve(APP_SECRET_KEY=LONG_KEY), LONG_KEY)

    def test_a_missing_key_refuses_to_start(self):
        with mock.patch.object(settings, "DEBUG", False):
            with self.assertRaises(ImproperlyConfigured) as caught:
                resolve()
            self.assertIn("APP_SECRET_KEY", str(caught.exception))

    def test_a_short_key_refuses_to_start(self):
        with mock.patch.object(settings, "DEBUG", False):
            with self.assertRaises(ImproperlyConfigured):
                resolve(APP_SECRET_KEY="dev")

    def test_the_admin_password_is_not_a_fallback(self):
        """This is the exact production state that caused the defect."""
        with mock.patch.object(settings, "DEBUG", False):
            with self.assertRaises(ImproperlyConfigured):
                resolve(ADMIN_PASSWORD="hunter2xy")

    def test_the_boundary_is_exactly_the_documented_length(self):
        with mock.patch.object(settings, "DEBUG", False):
            at_limit = "k" * settings.SECRET_KEY_MIN_LENGTH
            below = "k" * (settings.SECRET_KEY_MIN_LENGTH - 1)
            self.assertEqual(resolve(APP_SECRET_KEY=at_limit), at_limit)
            with self.assertRaises(ImproperlyConfigured):
                resolve(APP_SECRET_KEY=below)

    def test_debug_permits_a_placeholder(self):
        with mock.patch.object(settings, "DEBUG", True):
            self.assertEqual(resolve(), settings.SECRET_KEY_FALLBACK)

    def test_debug_still_prefers_a_real_key(self):
        with mock.patch.object(settings, "DEBUG", True):
            self.assertEqual(resolve(APP_SECRET_KEY="dev"), "dev")

    def test_whitespace_is_not_a_key(self):
        with mock.patch.object(settings, "DEBUG", False):
            with self.assertRaises(ImproperlyConfigured):
                resolve(APP_SECRET_KEY="   ")

    def test_the_running_configuration_satisfies_the_rule(self):
        """Whatever CI or local runs are configured with must actually pass.

        Without this, the guard could be satisfied only in tests that patch the
        environment while the real entry points keep a placeholder too short to
        start.
        """
        if settings.DEBUG:
            self.skipTest("DEBUG permits the placeholder")
        self.assertGreaterEqual(len(settings.SECRET_KEY), settings.SECRET_KEY_MIN_LENGTH)
