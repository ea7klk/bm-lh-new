from __future__ import annotations

from email.utils import formataddr
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from .i18n import translate


def _message(locale, subject_key, heading_key, intro_key, button_key, url, callsign, **values):
    subject = translate(locale, subject_key, **values)
    heading = translate(locale, heading_key, **values)
    greeting = translate(locale, "emailVerification.greeting", callsign=callsign)
    intro = translate(locale, intro_key, **values)
    button = translate(locale, button_key, **values)
    expiry = translate(locale, "emailVerification.expiry", **values)
    ignore = translate(locale, "emailVerification.ignore", **values)
    link_label = translate(locale, "emailVerification.linkLabel", **values)
    footer = translate(locale, "emailVerification.footer", **values)
    html = f"""
    <div style=\"font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#1f2937\">
      <div style=\"background:linear-gradient(135deg,#667eea,#764ba2);padding:28px;border-radius:14px 14px 0 0;color:white\"><h1 style=\"margin:0;font-size:24px\">{escape(heading)}</h1></div>
      <div style=\"padding:28px;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 14px 14px\">
        <p>{escape(greeting)}</p><p>{escape(intro)}</p>
        <p style=\"margin:28px 0\"><a href=\"{escape(url)}\" style=\"background:#667eea;color:white;padding:13px 20px;border-radius:8px;text-decoration:none;font-weight:bold\">{escape(button)}</a></p>
        <p style=\"color:#667085\">{escape(expiry)}</p><p style=\"color:#667085\">{escape(ignore)}</p>
        <p style=\"font-size:12px;color:#667085\">{escape(link_label)}<br>{escape(url)}</p>
        <hr style=\"border:0;border-top:1px solid #e5e7eb\"><p style=\"font-size:12px;color:#667085\">{escape(footer)}</p>
      </div>
    </div>"""
    text = f"{heading}\n\n{greeting}\n\n{intro}\n\n{url}\n\n{expiry}\n{ignore}\n\n{footer}"
    return subject, text, html


def send_account_email(locale, recipient, callsign, subject_key, heading_key, intro_key, button_key, path, token, **values):
    if not settings.SMTP_ENABLED:
        raise RuntimeError("SMTP email delivery is disabled")
    url = f"{settings.APP_PUBLIC_URL}{path}?token={token}"
    subject, text, html = _message(locale, subject_key, heading_key, intro_key, button_key, url, callsign, **values)
    sender = formataddr((settings.SMTP_FROM_NAME, settings.DEFAULT_FROM_EMAIL))
    headers = {"Reply-To": settings.SMTP_REPLY_TO} if settings.SMTP_REPLY_TO else None
    message = EmailMultiAlternatives(subject, text, sender, [recipient], headers=headers)
    message.attach_alternative(html, "text/html")
    message.send(fail_silently=False)


def send_verification(locale, user, token):
    send_account_email(locale, user.email, user.callsign, "emailVerification.subject", "emailVerification.heading", "emailVerification.intro", "emailVerification.button", "/verify/", token, hours=settings.EMAIL_VERIFICATION_HOURS)


def send_password_reset(locale, user, token):
    send_account_email(locale, user.email, user.callsign, "passwordReset.subject", "passwordReset.heading", "passwordReset.intro", "passwordReset.button", "/reset-password/", token, hours=settings.PASSWORD_RESET_HOURS)


def send_email_change(locale, recipient, callsign, token, stage, new_email):
    prefix = "old" if stage == "old" else "new"
    send_account_email(locale, recipient, callsign, f"emailChange.{prefix}Subject", f"emailChange.{prefix}Heading", f"emailChange.{prefix}Intro", f"emailChange.{prefix}Button", f"/change-email/confirm/{stage}/", token, hours=settings.EMAIL_VERIFICATION_HOURS, new_email=new_email)
