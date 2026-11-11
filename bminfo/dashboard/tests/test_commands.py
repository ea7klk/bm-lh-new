from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from dashboard.models import EmailVerificationToken, User


class BootstrapAdminCommandTests(TestCase):
    def test_bootstrap_admin_creates_configured_superuser(self):
        output = StringIO()
        with mock.patch.dict("os.environ", {"ADMIN_CALLSIGN": "ea7klk", "ADMIN_EMAIL": "admin@example.com", "ADMIN_PASSWORD": "strong-password"}, clear=False):
            call_command("bootstrap_admin", stdout=output)
        admin = User.objects.get(callsign="EA7KLK")
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.check_password("strong-password"))
        self.assertIn("Django administrator ready: EA7KLK", output.getvalue())

    def test_bootstrap_admin_requires_email_and_password(self):
        with mock.patch.dict("os.environ", {"ADMIN_EMAIL": "", "SMTP_USERNAME": "", "ADMIN_PASSWORD": ""}, clear=False):
            with self.assertRaises(CommandError):
                call_command("bootstrap_admin")


class PurgeUnverifiedUsersCommandTests(TestCase):
    def _user(self, callsign, *, is_active=False, verified=False, age_days=6, **extra):
        user = User.objects.create_user(callsign, f"{callsign.lower()}@example.com", "password", name=callsign, is_active=is_active, **extra)
        user.created_at = timezone.now() - timedelta(days=age_days)
        if verified:
            user.email_verified_at = user.created_at + timedelta(minutes=5)
        user.save(update_fields=["created_at", "email_verified_at"])
        return user

    def test_deletes_only_stale_unverified_accounts(self):
        stale = self._user("EA5OLD")
        fresh = self._user("EA5NEW", age_days=4)
        verified = self._user("EA5OK", is_active=True, verified=True, age_days=30)
        staff = self._user("EA5ADM", is_staff=True, age_days=30)
        output = StringIO()

        call_command("purge_unverified_users", stdout=output)

        self.assertFalse(User.objects.filter(pk=stale.pk).exists())
        self.assertTrue(User.objects.filter(pk=fresh.pk).exists())
        self.assertTrue(User.objects.filter(pk=verified.pk).exists())
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())
        self.assertIn("Deleted 1 unverified account(s)", output.getvalue())
        self.assertIn("EA5OLD", output.getvalue())

    def test_keeps_active_legacy_accounts_without_verification_stamp(self):
        # Accounts migrated from the legacy application are active but carry no
        # email_verified_at. They are real users and must never be purged.
        legacy = self._user("EA5LEGACY", is_active=True, verified=False, age_days=200)

        call_command("purge_unverified_users", stdout=StringIO())

        self.assertTrue(User.objects.filter(pk=legacy.pk).exists())

    def test_dry_run_reports_without_deleting(self):
        stale = self._user("EA5OLD")
        output = StringIO()

        call_command("purge_unverified_users", "--dry-run", stdout=output)

        self.assertTrue(User.objects.filter(pk=stale.pk).exists())
        self.assertIn("Would delete 1 unverified account(s)", output.getvalue())

    def test_days_option_overrides_retention_setting(self):
        two_days_old = self._user("EA5TWO", age_days=2)
        output = StringIO()

        call_command("purge_unverified_users", "--days", "1", stdout=output)

        self.assertFalse(User.objects.filter(pk=two_days_old.pk).exists())

    @override_settings(UNVERIFIED_USER_RETENTION_DAYS=7)
    def test_uses_configured_retention_when_days_is_omitted(self):
        six_days_old = self._user("EA5SIX", age_days=6)

        call_command("purge_unverified_users", stdout=StringIO())

        self.assertTrue(User.objects.filter(pk=six_days_old.pk).exists())

    def test_deletes_related_verification_tokens(self):
        stale = self._user("EA5OLD")
        EmailVerificationToken.objects.create(user=stale, token_hash="stale-token", expires_at=timezone.now() + timedelta(hours=1))

        call_command("purge_unverified_users", stdout=StringIO())

        self.assertFalse(EmailVerificationToken.objects.filter(user_id=stale.pk).exists())

    def test_rejects_retention_below_one_day(self):
        with self.assertRaises(CommandError):
            call_command("purge_unverified_users", "--days", "0", stdout=StringIO())
