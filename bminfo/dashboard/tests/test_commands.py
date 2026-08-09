from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from dashboard.models import User


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
