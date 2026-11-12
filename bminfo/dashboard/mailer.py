from __future__ import annotations

from email.utils import formataddr
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from .i18n import translate, translate_count


def _message(locale, section, subject_key, heading_key, intro_key, button_key, url, callsign, **values):
    """Render one account email.

    Every key except the four named arguments is read from ``section`` so that
    each email type uses its own wording: a password-reset message says so,
    rather than borrowing the confirmation wording the way it used to.
    """
    subject = translate(locale, subject_key, **values)
    heading = translate(locale, heading_key, **values)
    greeting = translate(locale, f"{section}.greeting", callsign=callsign)
    intro = translate(locale, intro_key, **values)
    button = translate(locale, button_key, **values)
    hours = values.get("hours")
    if hours is None:
        expiry = translate(locale, f"{section}.expiryOther", **values)
    else:
        expiry = translate_count(locale, f"{section}.expiry", hours, **values)
    ignore = translate(locale, f"{section}.ignore", **values)
    link_label = translate(locale, f"{section}.linkLabel", **values)
    footer = translate(locale, f"{section}.footer", **values)
    disclaimer = translate(locale, f"{section}.disclaimer", **values)

    # The accent matches the site palette; the previous purple gradient predated
    # the rebrand.
    html = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:620px;margin:auto;color:#3f4a54">
      <div style="background:#dd4b39;padding:26px 28px;border-radius:4px 4px 0 0;color:#fff">
        <h1 style="margin:0;font-size:22px;font-weight:600;line-height:1.3">{escape(heading)}</h1>
      </div>
      <div style="padding:28px;border:1px solid #d2d6de;border-top:0;border-radius:0 0 4px 4px">
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6">{escape(greeting)}</p>
        <p style="margin:0 0 20px;font-size:15px;line-height:1.6">{escape(intro)}</p>
        <p style="margin:24px 0">
          <a href="{escape(url)}" style="background:#dd4b39;color:#fff;padding:12px 20px;border-radius:3px;text-decoration:none;font-weight:600;display:inline-block">{escape(button)}</a>
        </p>
        <p style="margin:0 0 6px;color:#7b8794;font-size:13px;line-height:1.6">{escape(expiry)}</p>
        <p style="margin:0 0 18px;color:#7b8794;font-size:13px;line-height:1.6">{escape(ignore)}</p>
        <p style="margin:0;font-size:12px;color:#7b8794;line-height:1.6">{escape(link_label)}<br>
          <span style="word-break:break-all;color:#3c8dbc">{escape(url)}</span></p>
      </div>
      <div style="border-top:1px solid #e3e7ec;margin-top:18px;padding-top:14px">
        <p style="margin:0 0 8px;font-size:12px;color:#7b8794;line-height:1.6">{escape(footer)}</p>
        <p style="margin:0;font-size:11px;color:#8a949c;line-height:1.65">{escape(disclaimer)}</p>
      </div>
    </div>"""
    text = (
        f"{heading}\n\n{greeting}\n\n{intro}\n\n{url}\n\n"
        f"{expiry}\n{ignore}\n\n{link_label}\n{url}\n\n"
        f"-- \n{footer}\n{disclaimer}\n"
    )
    return subject, text, html


def send_account_email(locale, recipient, callsign, section, subject_key, heading_key, intro_key, button_key, path, token, **values):
    if not settings.SMTP_ENABLED:
        raise RuntimeError("SMTP email delivery is disabled")
    url = f"{settings.APP_PUBLIC_URL}{path}?token={token}"
    subject, text, html = _message(locale, section, subject_key, heading_key, intro_key, button_key, url, callsign, **values)
    sender = formataddr((settings.SMTP_FROM_NAME, settings.DEFAULT_FROM_EMAIL))
    headers = {"Reply-To": settings.SMTP_REPLY_TO} if settings.SMTP_REPLY_TO else None
    message = EmailMultiAlternatives(subject, text, sender, [recipient], headers=headers)
    message.attach_alternative(html, "text/html")
    message.send(fail_silently=False)


def send_verification(locale, user, token):
    send_account_email(
        locale, user.email, user.callsign, "emailVerification",
        "emailVerification.subject", "emailVerification.heading",
        "emailVerification.intro", "emailVerification.button",
        "/verify/", token, hours=settings.EMAIL_VERIFICATION_HOURS,
    )


def send_password_reset(locale, user, token):
    send_account_email(
        locale, user.email, user.callsign, "passwordReset",
        "passwordReset.subject", "passwordReset.heading",
        "passwordReset.intro", "passwordReset.button",
        "/reset-password/", token, hours=settings.PASSWORD_RESET_HOURS,
    )


def send_email_change(locale, recipient, callsign, token, stage, new_email):
    prefix = "old" if stage == "old" else "new"
    send_account_email(
        locale, recipient, callsign, "emailChange",
        f"emailChange.{prefix}Subject", f"emailChange.{prefix}Heading",
        f"emailChange.{prefix}Intro", f"emailChange.{prefix}Button",
        f"/change-email/confirm/{stage}/", token,
        hours=settings.EMAIL_VERIFICATION_HOURS, new_email=new_email,
    )
