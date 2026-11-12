from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.utils.html import escape

from dashboard.i18n import SUPPORTED_LOCALES, catalog, plural_category, translate_count
from dashboard.mailer import _message


class PluralTests(SimpleTestCase):
    def test_english_spanish_and_german_pluralise_on_one(self):
        for locale in ("en", "es", "de"):
            with self.subTest(locale=locale):
                self.assertEqual(plural_category(locale, 0), "other")
                self.assertEqual(plural_category(locale, 1), "one")
                self.assertEqual(plural_category(locale, 2), "other")

    def test_french_treats_zero_as_singular(self):
        self.assertEqual(plural_category("fr", 0), "one")
        self.assertEqual(plural_category("fr", 1), "one")
        self.assertEqual(plural_category("fr", 2), "other")

    def test_expiry_lines_use_the_right_number_form(self):
        cases = {
            "en": {1: "in 1 hour.", 2: "in 2 hours.", 48: "in 48 hours."},
            "es": {1: "en 1 hora.", 2: "en 2 horas.", 48: "en 48 horas."},
            "de": {1: "in 1 Stunde ab.", 2: "in 2 Stunden ab.", 48: "in 48 Stunden ab."},
            "fr": {0: "dans 0 heure.", 1: "dans 1 heure.", 2: "dans 2 heures."},
        }
        for locale, expectations in cases.items():
            for count, fragment in expectations.items():
                with self.subTest(locale=locale, count=count):
                    line = translate_count(locale, "emailVerification.expiry", count, hours=count)
                    self.assertTrue(line.endswith(fragment), f"{line!r} should end with {fragment!r}")

    def test_plural_keys_exist_in_every_locale(self):
        for locale in SUPPORTED_LOCALES:
            for section in ("emailVerification", "passwordReset", "emailChange"):
                with self.subTest(locale=locale, section=section):
                    keys = catalog(locale)[section]
                    self.assertIn("expiryOne", keys)
                    self.assertIn("expiryOther", keys)
                    self.assertNotIn("expiry", keys, "the un-pluralised key should be gone")


class EmailWordingTests(SimpleTestCase):
    """Each email must speak for itself, not borrow another email's wording."""

    def _render(self, locale, section, hours):
        # emailChange has two variants (old/new); use the "old" one as the
        # representative wording. Its keys are camel-cased, the others are not.
        if section == "emailChange":
            keys = (
                "emailChange.oldSubject",
                "emailChange.oldHeading",
                "emailChange.oldIntro",
                "emailChange.oldButton",
            )
        else:
            keys = (
                f"{section}.subject",
                f"{section}.heading",
                f"{section}.intro",
                f"{section}.button",
            )
        return _message(
            locale, section, *keys, "https://example.test/x?token=t", "EA7KLK",
            hours=hours, new_email="new@example.test",
        )

    def test_password_reset_does_not_reuse_confirmation_wording(self):
        leaked = {
            "en": "confirmation link",
            "es": "enlace de confirmación",
            "de": "Bestätigungslink",
            "fr": "lien de confirmation",
        }
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                _, text, html = self._render(locale, "passwordReset", 1)
                self.assertNotIn(leaked[locale], text)
                self.assertNotIn(leaked[locale], html)

    def test_each_email_mentions_its_own_purpose(self):
        expected = {
            "en": {"emailVerification": "confirm", "passwordReset": "reset", "emailChange": "email"},
            "es": {"emailVerification": "confirm", "passwordReset": "restablecer", "emailChange": "correo"},
            "de": {"emailVerification": "bestätig", "passwordReset": "zurücksetzen", "emailChange": "e-mail"},
            "fr": {"emailVerification": "confirm", "passwordReset": "réinitialisation", "emailChange": "e-mail"},
        }
        for locale, sections in expected.items():
            for section, fragment in sections.items():
                with self.subTest(locale=locale, section=section):
                    _, text, _ = self._render(locale, section, 24)
                    self.assertIn(fragment.lower(), text.lower())

    def test_emails_use_the_site_name_rather_than_the_old_wordmark(self):
        for locale in SUPPORTED_LOCALES:
            for section in ("emailVerification", "passwordReset", "emailChange"):
                with self.subTest(locale=locale, section=section):
                    subject, text, html = self._render(locale, section, 24)
                    every_part = subject + text + html
                    self.assertNotIn("BrandMeister Lastheard", every_part)
                    self.assertNotIn("emailChange.heading", every_part, "untranslated key leaked")
                    self.assertIn(catalog(locale)["common"]["appName"], every_part)

    def test_emails_carry_the_non_affiliation_notice(self):
        for locale in SUPPORTED_LOCALES:
            for section in ("emailVerification", "passwordReset", "emailChange"):
                with self.subTest(locale=locale, section=section):
                    notice = catalog(locale)[section]["disclaimer"]
                    _, text, html = self._render(locale, section, 24)
                    self.assertIn(notice, text)
                    # The HTML part escapes apostrophes, so compare against the
                    # escaped form.
                    self.assertIn(escape(notice), html)

    def test_email_links_point_at_the_configured_public_url(self):
        with override_settings(APP_PUBLIC_URL="https://example.test"):
            _, text, html = self._render("en", "emailVerification", 48)
        self.assertIn("https://example.test/x", text)

    def test_sender_display_name_matches_the_site(self):
        self.assertEqual(settings.SMTP_FROM_NAME, "BrandMeister Statistics by EA7KLK")
