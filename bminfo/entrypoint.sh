#!/bin/sh
set -eu

python manage.py migrate
python manage.py collectstatic --noinput
python manage.py bootstrap_admin

collector_pid=""
if [ "${DJANGO_COLLECTOR_ENABLED:-true}" = "true" ]; then
  python manage.py collect_brandmeister &
  collector_pid=$!
fi

cleanup() {
  if [ -n "$collector_pid" ]; then
    kill -TERM "$collector_pid" 2>/dev/null || true
    wait "$collector_pid" 2>/dev/null || true
  fi
}
trap cleanup TERM INT EXIT

daphne -b 0.0.0.0 -p 8000 \
  config.asgi:application &
web_pid=$!
wait "$web_pid"
