import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

django_application = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from dashboard.routing import websocket_urlpatterns


async def lifespan_application(scope, receive, send):
    """Answer the ASGI lifespan protocol.

    Neither Django nor Channels implements it: ``ASGIHandler`` refuses any scope
    that is not HTTP, and ``ProtocolTypeRouter`` raises for a scope type it has
    no mapping for. Under Uvicorn that surfaces as
    ``ASGI 'lifespan' protocol appears unsupported`` on every worker start, and
    — less visibly — Uvicorn then sets ``error_occurred``, which makes it skip
    the shutdown handshake entirely. No application shutdown hook could ever
    run.

    Acknowledging both halves keeps the log honest and leaves the shutdown path
    available for future use. There is no startup work here: migrations and
    static collection run in ``entrypoint.sh`` before gunicorn starts, and the
    collector holds the only long-lived connection.

    If startup work is ever added, do it here and send
    ``lifespan.startup.failed`` with a message on error rather than allowing the
    exception to escape, so a misconfigured pod fails its startup probe with a
    reason instead of serving traffic in a broken state.
    """
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


application = ProtocolTypeRouter(
    {
        "http": django_application,
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
        "lifespan": lifespan_application,
    }
)

# Gunicorn/Uvicorn serves the Django app directly in the Compose stack. Wrap the ASGI
# application so admin and other static assets are available without a
# separate web server. Non-HTTP scopes (websocket, lifespan) pass straight
# through to the router above.
application = ASGIStaticFilesHandler(application)
