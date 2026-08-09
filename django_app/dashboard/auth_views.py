from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .forms import CallsignAuthenticationForm, PasswordChangeForm, RegistrationForm
from .i18n import locale_from_request, translate
from .mailer import send_email_change, send_password_reset, send_verification
from .models import EmailChangeRequest, EmailVerificationToken, PasswordResetToken, User, UserSession


def _token():
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _account(request, template, title_key, **context):
    locale = locale_from_request(request)
    context.update({"title": translate(locale, title_key), "locale": locale})
    response = render(request, template, context)
    if request.GET.get("lang") in {"en", "es", "de", "fr"}:
        response.set_cookie("bm_lang", request.GET["lang"], max_age=31536000, samesite="Lax")
    return response


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = CallsignAuthenticationForm(request, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        User.objects.filter(pk=user.pk).update(last_login_at=timezone.now())
        return redirect(request.POST.get("next") or "dashboard")
    return _account(request, "registration/login.html", "user.loginTitle", form=form, next=request.GET.get("next", ""))


def logout_view(request):
    if request.user.is_authenticated:
        # UserSession contains application sessions migrated from the legacy
        # app. It is not tied to Django's session key, so remove the user's
        # records explicitly; django.contrib.auth.logout() then flushes the
        # current Django session as usual.
        UserSession.objects.filter(user_id=request.user.pk).delete()
    logout(request)
    return redirect("dashboard")


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        user = User.objects.create_user(data["callsign"], data["email"], data["password"], name=data["name"], is_active=False)
        raw, hashed = _token()
        EmailVerificationToken.objects.filter(user=user).delete()
        from django.conf import settings
        EmailVerificationToken.objects.create(user=user, token_hash=hashed, expires_at=timezone.now() + timedelta(hours=settings.EMAIL_VERIFICATION_HOURS))
        try:
            send_verification(locale_from_request(request), user, raw)
        except Exception:
            user.delete()
            messages.error(request, "We could not send the confirmation email. Please try again later.")
            return _account(request, "registration/register.html", "user.register", form=form)
        messages.success(request, translate(locale_from_request(request), "emailVerification.sent"))
        return _account(request, "registration/register.html", "emailVerification.sentTitle", form=None, completed=True)
    return _account(request, "registration/register.html", "user.register", form=form)


def verify_email(request):
    locale = locale_from_request(request)
    token = request.GET.get("token", "")
    hashed = hashlib.sha256(token.encode()).hexdigest() if token else ""
    with transaction.atomic():
        record = EmailVerificationToken.objects.select_for_update().filter(token_hash=hashed, consumed_at__isnull=True, expires_at__gt=timezone.now()).select_related("user").first()
        if record is None:
            return _account(request, "registration/message.html", "emailVerification.invalidTitle", message=translate(locale, "emailVerification.invalid"), error=True)
        record.consumed_at = timezone.now()
        record.save(update_fields=["consumed_at"])
        User.objects.filter(pk=record.user_id).update(is_active=True, email_verified_at=timezone.now())
    return _account(request, "registration/message.html", "emailVerification.successTitle", message=translate(locale, "emailVerification.success"), success=True)


def forgot_password(request):
    locale = locale_from_request(request)
    if request.method == "GET":
        return _account(request, "registration/forgot_password.html", "user.forgotPasswordTitle")
    email = request.POST.get("email", "").strip().lower()
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user:
        raw, hashed = _token()
        PasswordResetToken.objects.filter(user=user).delete()
        from django.conf import settings
        PasswordResetToken.objects.create(user=user, token_hash=hashed, expires_at=timezone.now() + timedelta(hours=settings.PASSWORD_RESET_HOURS))
        try:
            send_password_reset(locale, user, raw)
        except Exception:
            messages.error(request, translate(locale, "user.passwordResetDeliveryFailed"))
            return _account(request, "registration/forgot_password.html", "user.forgotPasswordTitle")
    return _account(request, "registration/message.html", "user.resetEmailSentTitle", message=translate(locale, "user.resetEmailSent"), success=True)


def reset_password(request):
    locale = locale_from_request(request)
    raw = request.GET.get("token") if request.method == "GET" else request.POST.get("token")
    hashed = hashlib.sha256((raw or "").encode()).hexdigest()
    record = PasswordResetToken.objects.filter(token_hash=hashed, consumed_at__isnull=True, expires_at__gt=timezone.now()).select_related("user").first()
    if record is None:
        return _account(request, "registration/message.html", "user.resetInvalidTitle", message=translate(locale, "user.resetInvalid"), error=True)
    if request.method == "GET":
        return _account(request, "registration/reset_password.html", "user.resetPasswordTitle", token=raw)
    password = request.POST.get("password", "")
    confirmation = request.POST.get("confirm_password", "")
    if len(password) < 8 or password != confirmation:
        messages.error(request, translate(locale, "user.passwordMismatch" if password != confirmation else "user.passwordMin"))
        return _account(request, "registration/reset_password.html", "user.resetPasswordTitle", token=raw)
    record.user.set_password(password)
    record.user.save(update_fields=["password"])
    record.consumed_at = timezone.now()
    record.save(update_fields=["consumed_at"])
    return _account(request, "registration/message.html", "user.resetPasswordTitle", message=translate(locale, "user.passwordResetSuccess"), success=True)


@login_required
def profile(request):
    from django.db.models import Count, Sum, Max
    from .models import QSO
    qsos = QSO.objects.filter(source_call__iexact=request.user.callsign)
    stats = qsos.aggregate(count=Count("session_id"), duration=Sum("duration_ms"), talkgroups=Count("talkgroup_id", distinct=True), last=Max("start_at"))
    top_talkgroups = list(qsos.values("talkgroup_id", "destination_name").annotate(count=Count("session_id"), duration=Sum("duration_ms")).order_by("-count")[:10])
    return _account(request, "registration/profile.html", "user.profile", stats=stats, top_talkgroups=top_talkgroups, change_form=PasswordChangeForm(), user=request.user)


@login_required
def api_stats(request):
    from django.db.models import Count, Sum, Max
    from .models import QSO
    qsos = QSO.objects.filter(source_call__iexact=request.user.callsign)
    stats = qsos.aggregate(qso_count=Count("session_id"), duration_ms=Sum("duration_ms"), unique_talkgroups=Count("talkgroup_id", distinct=True), last_qso_at=Max("start_at"))
    return JsonResponse({"qso_count": stats["qso_count"] or 0, "duration_seconds": (stats["duration_ms"] or 0) / 1000, "unique_talkgroups": stats["unique_talkgroups"] or 0, "last_qso_at": stats["last_qso_at"].isoformat() if stats["last_qso_at"] else None})


@login_required
@require_http_methods(["POST"])
def change_password(request):
    form = PasswordChangeForm(request.POST)
    if form.is_valid() and request.user.check_password(form.cleaned_data["current_password"]):
        request.user.set_password(form.cleaned_data["new_password"])
        request.user.save(update_fields=["password"])
        messages.success(request, "Password changed successfully.")
    else:
        messages.error(request, "Current password is incorrect or the new passwords do not match.")
    return redirect("profile")


@login_required
@require_http_methods(["POST"])
def start_email_change(request):
    locale = locale_from_request(request)
    new_email = request.POST.get("new_email", "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new_email):
        messages.error(request, translate(locale, "user.emailChangeInvalidEmail"))
        return redirect("profile")
    if new_email == request.user.email.lower():
        messages.error(request, translate(locale, "user.emailChangeSame"))
        return redirect("profile")
    if User.objects.filter(email__iexact=new_email).exclude(pk=request.user.pk).exists():
        messages.error(request, translate(locale, "user.emailChangeDuplicate"))
        return redirect("profile")
    raw, hashed = _token()
    EmailChangeRequest.objects.filter(user=request.user).delete()
    from django.conf import settings
    change = EmailChangeRequest.objects.create(user=request.user, old_email=request.user.email, new_email=new_email, old_token_hash=hashed, expires_at=timezone.now() + timedelta(hours=settings.EMAIL_VERIFICATION_HOURS))
    try:
        send_email_change(locale, request.user.email, request.user.callsign, raw, "old", new_email)
    except Exception:
        change.delete()
        messages.error(request, translate(locale, "user.emailChangeDeliveryFailed"))
    else:
        messages.success(request, translate(locale, "user.emailChangePending"))
    return redirect("profile")


def confirm_email_change(request, stage):
    locale = locale_from_request(request)
    if stage not in {"old", "new"}:
        return _account(request, "registration/message.html", "user.emailChangeInvalidTitle", message=translate(locale, "user.emailChangeInvalid"), error=True)
    raw = request.GET.get("token", "")
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    with transaction.atomic():
        change = EmailChangeRequest.objects.select_for_update().select_related("user").filter(expires_at__gt=timezone.now()).filter(**({"old_token_hash": hashed} if stage == "old" else {"new_token_hash": hashed})).first()
        if change is None:
            return _account(request, "registration/message.html", "user.emailChangeInvalidTitle", message=translate(locale, "user.emailChangeInvalid"), error=True)
        if stage == "old":
            raw_new, hashed_new = _token()
            change.old_confirmed_at = timezone.now()
            change.new_token_hash = hashed_new
            change.save(update_fields=["old_confirmed_at", "new_token_hash"])
            try:
                send_email_change(locale, change.new_email, change.user.callsign, raw_new, "new", change.new_email)
            except Exception:
                return _account(request, "registration/message.html", "user.emailChangeConfirmOldTitle", message=translate(locale, "user.emailChangeDeliveryFailed"), error=True)
            return _account(request, "registration/message.html", "user.emailChangeConfirmOldTitle", message=translate(locale, "user.emailChangeOldConfirmed"), success=True)
        if not change.old_confirmed_at:
            return _account(request, "registration/message.html", "user.emailChangeInvalidTitle", message=translate(locale, "user.emailChangeInvalid"), error=True)
        change.user.email = change.new_email
        change.user.save(update_fields=["email"])
        change.new_confirmed_at = timezone.now()
        change.save(update_fields=["new_confirmed_at"])
        change.delete()
    return _account(request, "registration/message.html", "user.emailChangeSuccessTitle", message=translate(locale, "user.emailChangeSuccess"), success=True)
