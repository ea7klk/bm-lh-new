import re

from django import forms
from django.contrib.auth import authenticate

from .models import User


class CallsignAuthenticationForm(forms.Form):
    username = forms.CharField(label="Email or callsign", max_length=240)
    password = forms.CharField(label="Password", strip=False, widget=forms.PasswordInput)

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        login_value = cleaned.get("username", "").strip()
        password = cleaned.get("password", "")
        if not login_value or not password:
            return cleaned
        user = User.objects.filter(callsign__iexact=login_value).first()
        if user is None:
            user = User.objects.filter(email__iexact=login_value).first()
        if user is not None and user.check_password(password):
            if not user.is_active:
                raise forms.ValidationError("Please confirm your email address before logging in.")
            self.user_cache = user
            return cleaned
        raise forms.ValidationError("Invalid email/callsign or password.")

    def get_user(self):
        return self.user_cache


class RegistrationForm(forms.Form):
    callsign = forms.CharField(max_length=16)
    name = forms.CharField(max_length=120)
    email = forms.EmailField(max_length=240)
    password = forms.CharField(min_length=8, widget=forms.PasswordInput)
    confirm_password = forms.CharField(min_length=8, widget=forms.PasswordInput)

    def clean_callsign(self):
        callsign = self.cleaned_data["callsign"].strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9/-]{1,15}", callsign):
            raise forms.ValidationError("Enter a valid callsign.")
        if User.objects.filter(callsign__iexact=callsign).exists():
            raise forms.ValidationError("That callsign or email is already registered.")
        return callsign

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("That callsign or email is already registered.")
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("confirm_password"):
            self.add_error("confirm_password", "The passwords do not match.")
        return cleaned


class PasswordChangeForm(forms.Form):
    current_password = forms.CharField(widget=forms.PasswordInput)
    new_password = forms.CharField(min_length=8, widget=forms.PasswordInput)
    confirm_password = forms.CharField(min_length=8, widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("new_password") != cleaned.get("confirm_password"):
            self.add_error("confirm_password", "The passwords do not match.")
        return cleaned
