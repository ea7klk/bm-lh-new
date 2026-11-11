from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from dashboard.models import User


class UserAdminChangelistTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("ADMIN", "admin@example.com", "admin-password", name="Administrator")
        self.client.force_login(self.admin)

    def test_changelist_lists_created_at_column(self):
        user = User.objects.create_user("EA5NEW", "ea5new@example.com", "new-password", name="New Operator")
        User.objects.filter(pk=user.pk).update(created_at=datetime(2020, 3, 4, 5, 6, 7, tzinfo=dt_timezone.utc))

        response = self.client.get("/admin/dashboard/user/")

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("created at", content.lower())
        self.assertIn("2020", content)
