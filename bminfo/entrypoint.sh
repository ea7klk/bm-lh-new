#!/bin/sh
set -eu

python manage.py migrate
python manage.py collectstatic --noinput
python manage.py bootstrap_admin

collector_pid=""
web_pid=""
if [ "${DJANGO_COLLECTOR_ENABLED:-true}" = "true" ]; then
  python manage.py collect_brandmeister &
  collector_pid=$!
fi

cleanup() {
  if [ -n "$web_pid" ]; then
    kill -TERM "$web_pid" 2>/dev/null || true
    wait "$web_pid" 2>/dev/null || true
  fi
  if [ -n "$collector_pid" ]; then
    kill -TERM "$collector_pid" 2>/dev/null || true
    wait "$collector_pid" 2>/dev/null || true
  fi
}
trap cleanup TERM INT EXIT

gunicorn \
  --bind 0.0.0.0:8000 \
  --workers "${DJANGO_WORKERS:-3}" \
  --worker-class uvicorn.workers.UvicornWorker \
  --timeout "${DJANGO_TIMEOUT_SECONDS:-120}" \
  --graceful-timeout "${DJANGO_GRACEFUL_TIMEOUT_SECONDS:-30}" \
  --keep-alive "${DJANGO_KEEPALIVE_SECONDS:-5}" \
  config.asgi:application &
web_pid=$!
wait "$web_pid"
