"""Middleware that keeps the Django admin in the same language as the app.

The application translates its own strings from ``translations.json`` inside a
context processor, which needs no activation. Django's admin chrome does not
work that way: its labels come from Django's own compiled catalogues and only
switch when a locale is activated for the request.

Without this, an admin page for a Spanish or German visitor renders the app's
translated wordmark above Django's English "Log in", "Add" and "Change" --
mixed language, which reads worse than either language on its own.

Scoped to the admin path on purpose. Activating a locale globally would also
switch Django's date, time and number formats on the statistics pages, and those
deliberately format their own output.
"""

from django.utils import translation

from .i18n import locale_from_request


class AdminLocaleMiddleware:
    ADMIN_PREFIX = "/admin/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(self.ADMIN_PREFIX):
            return self.get_response(request)

        locale = locale_from_request(request)
        # override() restores the previous language on exit, so the activation
        # cannot leak into the next request on this thread.
        with translation.override(locale):
            request.LANGUAGE_CODE = locale
            return self.get_response(request)
