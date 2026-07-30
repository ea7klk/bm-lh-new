from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from smtplib import SMTP, SMTP_SSL, SMTPException
import ssl
from urllib.parse import quote

from .auth import new_email_verification_token, new_password_reset_token
from .config import settings
from .i18n import normalize_locale, translate


class EmailDeliveryError(RuntimeError):
    """Raised when an application email cannot be delivered."""


def verification_url(token: str, locale: str) -> str:
    locale = normalize_locale(locale) or "en"
    return (
        f"{settings.app_public_url}/user/verify?token={quote(token)}"
        f"&lang={quote(locale)}"
    )


def password_reset_url(token: str, locale: str) -> str:
    locale = normalize_locale(locale) or "en"
    return (
        f"{settings.app_public_url}/user/reset-password?token={quote(token)}"
        f"&lang={quote(locale)}"
    )


def email_change_url(token: str, stage: str, locale: str) -> str:
    locale = normalize_locale(locale) or "en"
    stage = "new" if stage == "new" else "old"
    return (
        f"{settings.app_public_url}/user/change-email/confirm?token={quote(token)}"
        f"&stage={stage}&lang={quote(locale)}"
    )


def render_verification_email(
    callsign: str,
    token: str,
    locale: str,
) -> tuple[str, str, str]:
    locale = normalize_locale(locale) or "en"
    url = verification_url(token, locale)
    safe_callsign = escape(callsign)
    safe_url = escape(url, quote=True)
    subject = translate(locale, "emailVerification.subject")
    heading = translate(locale, "emailVerification.heading")
    greeting = translate(locale, "emailVerification.greeting").format(callsign=safe_callsign)
    intro = translate(locale, "emailVerification.intro")
    button = translate(locale, "emailVerification.button")
    expiry = translate(locale, "emailVerification.expiry").format(
        hours=settings.email_verification_hours
    )
    ignore = translate(locale, "emailVerification.ignore")
    footer = translate(locale, "emailVerification.footer")
    link_label = translate(locale, "emailVerification.linkLabel")

    html_body = f"""<!doctype html>
<html lang="{escape(locale)}">
  <body style="margin:0;background:#f1f3ff;color:#1f2937;font-family:Arial,Helvetica,sans-serif;">
    <div style="padding:32px 12px;">
      <div style="max-width:620px;margin:0 auto;background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 14px 40px rgba(31,41,55,.16);">
        <div style="padding:28px 32px;background:linear-gradient(135deg,#667eea,#764ba2);color:#ffffff;">
          <div style="font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.85;">BrandMeister Lastheard</div>
          <h1 style="margin:12px 0 0;font-size:28px;line-height:1.2;">{escape(heading)}</h1>
        </div>
        <div style="padding:32px;">
          <p style="margin:0 0 16px;font-size:17px;">{escape(greeting)}</p>
          <p style="margin:0 0 24px;line-height:1.65;color:#4b5563;">{escape(intro)}</p>
          <p style="margin:0 0 26px;text-align:center;">
            <a href="{safe_url}" style="display:inline-block;padding:14px 24px;border-radius:9px;background:linear-gradient(135deg,#667eea,#764ba2);color:#ffffff;text-decoration:none;font-weight:700;">{escape(button)}</a>
          </p>
          <p style="margin:0 0 12px;font-size:13px;color:#6b7280;">{escape(expiry)}</p>
          <p style="margin:0 0 12px;font-size:13px;color:#6b7280;">{escape(ignore)}</p>
          <p style="margin:0;font-size:12px;line-height:1.5;color:#9ca3af;">{escape(link_label)}<br><a href="{safe_url}" style="color:#5b5bd6;word-break:break-all;">{safe_url}</a></p>
        </div>
        <div style="padding:18px 32px;background:#f8f9ff;color:#6b7280;font-size:12px;line-height:1.5;">{escape(footer)}</div>
      </div>
    </div>
  </body>
</html>"""
    text_body = "\n\n".join((heading, greeting, intro, url, expiry, ignore, footer))
    return subject, text_body, html_body


def render_password_reset_email(
    callsign: str,
    token: str,
    locale: str,
) -> tuple[str, str, str]:
    locale = normalize_locale(locale) or "en"
    url = password_reset_url(token, locale)
    safe_callsign = escape(callsign)
    safe_url = escape(url, quote=True)
    subject = translate(locale, "passwordReset.subject")
    heading = translate(locale, "passwordReset.heading")
    greeting = translate(locale, "passwordReset.greeting").format(callsign=safe_callsign)
    intro = translate(locale, "passwordReset.intro")
    button = translate(locale, "passwordReset.button")
    expiry = translate(locale, "passwordReset.expiry").format(
        hours=settings.password_reset_hours
    )
    ignore = translate(locale, "passwordReset.ignore")
    footer = translate(locale, "passwordReset.footer")
    link_label = translate(locale, "passwordReset.linkLabel")

    html_body = f"""<!doctype html>
<html lang="{escape(locale)}">
  <body style="margin:0;background:#f1f3ff;color:#1f2937;font-family:Arial,Helvetica,sans-serif;">
    <div style="padding:32px 12px;">
      <div style="max-width:620px;margin:0 auto;background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 14px 40px rgba(31,41,55,.16);">
        <div style="padding:28px 32px;background:linear-gradient(135deg,#667eea,#764ba2);color:#ffffff;">
          <div style="font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.85;">BrandMeister Lastheard</div>
          <h1 style="margin:12px 0 0;font-size:28px;line-height:1.2;">{escape(heading)}</h1>
        </div>
        <div style="padding:32px;">
          <p style="margin:0 0 16px;font-size:17px;">{escape(greeting)}</p>
          <p style="margin:0 0 24px;line-height:1.65;color:#4b5563;">{escape(intro)}</p>
          <p style="margin:0 0 26px;text-align:center;">
            <a href="{safe_url}" style="display:inline-block;padding:14px 24px;border-radius:9px;background:linear-gradient(135deg,#667eea,#764ba2);color:#ffffff;text-decoration:none;font-weight:700;">{escape(button)}</a>
          </p>
          <p style="margin:0 0 12px;font-size:13px;color:#6b7280;">{escape(expiry)}</p>
          <p style="margin:0 0 12px;font-size:13px;color:#6b7280;">{escape(ignore)}</p>
          <p style="margin:0;font-size:12px;line-height:1.5;color:#9ca3af;">{escape(link_label)}<br><a href="{safe_url}" style="color:#5b5bd6;word-break:break-all;">{safe_url}</a></p>
        </div>
        <div style="padding:18px 32px;background:#f8f9ff;color:#6b7280;font-size:12px;line-height:1.5;">{escape(footer)}</div>
      </div>
    </div>
  </body>
</html>"""
    text_body = "\n\n".join((heading, greeting, intro, url, expiry, ignore, footer))
    return subject, text_body, html_body


def _send_message(message: EmailMessage) -> None:
    if not settings.smtp_enabled:
        raise EmailDeliveryError("SMTP delivery is disabled")
    if not settings.smtp_host or not settings.smtp_from_email:
        raise EmailDeliveryError("SMTP host and sender address are required")
    if settings.smtp_use_ssl and settings.smtp_use_tls:
        raise EmailDeliveryError("SMTP_USE_SSL and SMTP_USE_TLS cannot both be enabled")
    try:
        if settings.smtp_use_ssl:
            server = SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            server = SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            )
        with server:
            if settings.smtp_use_tls:
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except (OSError, SMTPException) as exc:
        raise EmailDeliveryError("Unable to deliver email") from exc


def _message(
    subject: str,
    email: str,
    text_body: str,
    html_body: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
    message["To"] = email
    if settings.smtp_reply_to:
        message["Reply-To"] = settings.smtp_reply_to
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def send_verification_email(
    email: str,
    callsign: str,
    locale: str,
    token: str | None = None,
) -> str:
    token = token or new_email_verification_token()
    subject, text_body, html_body = render_verification_email(callsign, token, locale)
    _send_message(_message(subject, email, text_body, html_body))
    return token


def send_password_reset_email(
    email: str,
    callsign: str,
    locale: str,
    token: str | None = None,
) -> str:
    token = token or new_password_reset_token()
    subject, text_body, html_body = render_password_reset_email(callsign, token, locale)
    _send_message(_message(subject, email, text_body, html_body))
    return token


def render_email_change_email(
    callsign: str,
    token: str,
    locale: str,
    stage: str,
    new_email: str = "",
) -> tuple[str, str, str]:
    """Render one of the two localized email-change confirmation messages."""
    locale = normalize_locale(locale) or "en"
    stage = "new" if stage == "new" else "old"
    url = email_change_url(token, stage, locale)
    prefix = f"{stage}"
    safe_callsign = escape(callsign)
    safe_new_email = escape(new_email)
    safe_url = escape(url, quote=True)
    subject = translate(locale, f"emailChange.{prefix}Subject")
    heading = translate(locale, f"emailChange.{prefix}Heading")
    greeting = translate(locale, "emailChange.greeting").format(callsign=safe_callsign)
    intro = translate(locale, f"emailChange.{prefix}Intro").format(new_email=safe_new_email)
    button = translate(locale, f"emailChange.{prefix}Button")
    expiry = translate(locale, "emailChange.expiry").format(
        hours=settings.email_verification_hours
    )
    ignore = translate(locale, "emailChange.ignore")
    footer = translate(locale, "emailChange.footer")
    link_label = translate(locale, "emailChange.linkLabel")

    html_body = f"""<!doctype html>
<html lang="{escape(locale)}">
  <body style="margin:0;background:#f1f3ff;color:#1f2937;font-family:Arial,Helvetica,sans-serif;">
    <div style="padding:32px 12px;">
      <div style="max-width:620px;margin:0 auto;background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 14px 40px rgba(31,41,55,.16);">
        <div style="padding:28px 32px;background:linear-gradient(135deg,#667eea,#764ba2);color:#ffffff;">
          <div style="font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.85;">BrandMeister Lastheard</div>
          <h1 style="margin:12px 0 0;font-size:28px;line-height:1.2;">{escape(heading)}</h1>
        </div>
        <div style="padding:32px;">
          <p style="margin:0 0 16px;font-size:17px;">{escape(greeting)}</p>
          <p style="margin:0 0 24px;line-height:1.65;color:#4b5563;">{escape(intro)}</p>
          <p style="margin:0 0 26px;text-align:center;">
            <a href="{safe_url}" style="display:inline-block;padding:14px 24px;border-radius:9px;background:linear-gradient(135deg,#667eea,#764ba2);color:#ffffff;text-decoration:none;font-weight:700;">{escape(button)}</a>
          </p>
          <p style="margin:0 0 12px;font-size:13px;color:#6b7280;">{escape(expiry)}</p>
          <p style="margin:0 0 12px;font-size:13px;color:#6b7280;">{escape(ignore)}</p>
          <p style="margin:0;font-size:12px;line-height:1.5;color:#9ca3af;">{escape(link_label)}<br><a href="{safe_url}" style="color:#5b5bd6;word-break:break-all;">{safe_url}</a></p>
        </div>
        <div style="padding:18px 32px;background:#f8f9ff;color:#6b7280;font-size:12px;line-height:1.5;">{escape(footer)}</div>
      </div>
    </div>
  </body>
</html>"""
    text_body = "\n\n".join((heading, greeting, intro, url, expiry, ignore, footer))
    return subject, text_body, html_body


def send_email_change_email(
    email: str,
    callsign: str,
    locale: str,
    token: str,
    stage: str,
    new_email: str = "",
) -> str:
    subject, text_body, html_body = render_email_change_email(
        callsign, token, locale, stage, new_email
    )
    _send_message(_message(subject, email, text_body, html_body))
    return token
