import os

from django.core.management.base import BaseCommand, CommandError

from dashboard.models import User


class Command(BaseCommand):
    help = "Create or update the configured Django administrator idempotently."

    def handle(self, *args, **options):
        callsign = os.getenv("ADMIN_CALLSIGN", "ADMIN").strip().upper()
        email = os.getenv("ADMIN_EMAIL") or os.getenv("SMTP_USERNAME", "")
        password = os.getenv("ADMIN_PASSWORD", "")
        if not email or not password:
            raise CommandError("ADMIN_EMAIL/SMTP_USERNAME and ADMIN_PASSWORD are required")
        user, _ = User.objects.get_or_create(callsign=callsign, defaults={"email": email, "name": "Administrator"})
        user.email = email
        user.name = user.name or "Administrator"
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Django administrator ready: {callsign}"))
