#!/bin/sh
# Container entrypoint with explicit roles.
#
#   ./entrypoint.sh web         migrations, static files, admin bootstrap, gunicorn
#   ./entrypoint.sh collector   migrations, then the LastHeard collector
#   ./entrypoint.sh task <cmd>  run one management command and exit (for Jobs)
#
# The web and collector pods start independently, so migrations are serialised
# with a PostgreSQL advisory lock instead of by starting order.
set -eu

ROLE="${1:-web}"
shift 2>/dev/null || true

case "$ROLE" in
  web)
    python manage.py db_migrate
    python manage.py collectstatic --noinput
    python manage.py bootstrap_admin
    # exec so SIGTERM reaches gunicorn directly; with no collector child there is
    # nothing to clean up, and graceful shutdown works without a shell in between.
    exec gunicorn \
      --bind 0.0.0.0:8000 \
      --workers "${APP_WORKERS:-3}" \
      --worker-class uvicorn.workers.UvicornWorker \
      --timeout "${APP_TIMEOUT_SECONDS:-120}" \
      --graceful-timeout "${APP_GRACEFUL_TIMEOUT_SECONDS:-30}" \
      --keep-alive "${APP_KEEPALIVE_SECONDS:-5}" \
      config.asgi:application
    ;;
  collector)
    python manage.py db_migrate
    exec python manage.py collect_brandmeister
    ;;
  task)
    exec python manage.py "$@"
    ;;
  *)
    echo "unknown role '$ROLE'; expected web, collector or task" >&2
    exit 64
    ;;
esac
