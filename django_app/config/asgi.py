import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

django_application = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from dashboard.routing import websocket_urlpatterns


application = ProtocolTypeRouter(
    {
        "http": django_application,
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)

# Daphne serves the Django app directly in the Compose stack. Wrap the ASGI
# application so admin and other static assets are available without a
# separate web server.
application = ASGIStaticFilesHandler(application)
