import hashlib
import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from dashboard.models import EmailChangeRequest, EmailVerificationToken, PasswordResetToken, User


class AuthenticationViewDatabaseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("EA7KLK", "ea7klk@example.com", "old-password", name="Volker")

    def test_registration_creates_inactive_user_and_verification_token(self):
        with mock.patch("dashboard.auth_views.send_verification") as send:
            response = self.client.post(
                "/register/",
                {"callsign": "EA5NEW", "name": "New Operator", "email": "new@example.com", "password": "new-password", "confirm_password": "new-password"},
            )
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(callsign="EA5NEW")
        self.assertFalse(user.is_active)
        self.assertEqual(EmailVerificationToken.objects.filter(user=user).count(), 1)
        send.assert_called_once()

    def test_registration_removes_user_when_email_delivery_fails(self):
        with mock.patch("dashboard.auth_views.send_verification", side_effect=RuntimeError("SMTP unavailable")):
            response = self.client.post(
                "/register/",
                {"callsign": "EA5FAIL", "name": "Failed Operator", "email": "fail@example.com", "password": "new-password", "confirm_password": "new-password"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(callsign="EA5FAIL").exists())

    def test_verify_email_activates_user_and_rejects_reuse(self):
        raw = "verification-token"
        token = EmailVerificationToken.objects.create(
            user=self.user,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        first = self.client.get("/verify/", {"token": raw})
        self.assertEqual(first.status_code, 200)
        self.user.refresh_from_db()
        token.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIsNotNone(token.consumed_at)
        self.assertEqual(self.client.get("/verify/", {"token": raw}).status_code, 200)

    def test_login_and_logout_manage_session(self):
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        response = self.client.post("/login/", {"username": "EA7KLK", "password": "old-password"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertEqual(self.client.get("/profile/").status_code, 200)
        self.assertEqual(self.client.get("/logout/").status_code, 302)
        self.assertEqual(self.client.get("/profile/").status_code, 302)

    def test_password_reset_creates_token_and_updates_password(self):
        with mock.patch("dashboard.auth_views.send_password_reset") as send:
            response = self.client.post("/forgot-password/", {"email": self.user.email})
        self.assertEqual(response.status_code, 200)
        send.assert_called_once()
        token = PasswordResetToken.objects.get(user=self.user)
        raw = "reset-token"
        token.token_hash = hashlib.sha256(raw.encode()).hexdigest()
        token.save(update_fields=["token_hash"])
        self.assertEqual(self.client.get("/reset-password/", {"token": raw}).status_code, 200)
        response = self.client.post("/reset-password/", {"token": raw, "password": "new-password", "confirm_password": "new-password"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-password"))
        self.assertTrue(PasswordResetToken.objects.get(pk=token.pk).consumed_at)

    def test_profile_stats_api_and_change_password(self):
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/profile/").status_code, 200)
        stats = json.loads(self.client.get("/user/api/stats/").content)
        self.assertEqual(stats["qso_count"], 0)
        response = self.client.post("/change-password/", {"current_password": "old-password", "new_password": "changed-password", "confirm_password": "changed-password"})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("changed-password"))

    def test_email_change_requires_confirmation_stages(self):
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.client.force_login(self.user)
        raw_old = "old-email-token"
        with mock.patch("dashboard.auth_views._token", return_value=(raw_old, hashlib.sha256(raw_old.encode()).hexdigest())), mock.patch("dashboard.auth_views.send_email_change"):
            response = self.client.post("/change-email/", {"new_email": "new-email@example.com"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EmailChangeRequest.objects.filter(user=self.user).exists())
        raw_new = "new-email-token"
        with mock.patch("dashboard.auth_views._token", return_value=(raw_new, hashlib.sha256(raw_new.encode()).hexdigest())), mock.patch("dashboard.auth_views.send_email_change"):
            confirmed = self.client.get("/change-email/confirm/old/", {"token": raw_old})
        self.assertEqual(confirmed.status_code, 200)
        with mock.patch("dashboard.auth_views.send_email_change"):
            completed = self.client.get("/change-email/confirm/new/", {"token": raw_new})
        self.assertEqual(completed.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new-email@example.com")
        self.assertFalse(EmailChangeRequest.objects.filter(user=self.user).exists())
