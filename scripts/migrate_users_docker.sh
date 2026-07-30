#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 export|import [migration arguments...]" >&2
    echo "Set MIGRATION_DIR to change the host directory used for the export file." >&2
    exit 64
fi

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATION_DIR="${MIGRATION_DIR:-$PROJECT_DIR/migration-data}"
MIGRATION_SERVICE="${MIGRATION_SERVICE:-web}"

mkdir -p "$MIGRATION_DIR"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "Docker Compose was not found; install Docker Compose before running this helper." >&2
    exit 127
fi

# Pass the source DSN explicitly when it is supplied through the host environment. The target
# DATABASE_URL is already provided by the Compose web service and points at the postgres service.
RUN_ARGS=(run --rm --no-deps)
if [[ -n "${REFERENCE_DATABASE_URL:-}" ]]; then
    RUN_ARGS+=(-e "REFERENCE_DATABASE_URL=${REFERENCE_DATABASE_URL}")
fi
RUN_ARGS+=(-v "$MIGRATION_DIR:/migration" "$MIGRATION_SERVICE")

"${COMPOSE[@]}" "${RUN_ARGS[@]}" \
    python /app/scripts/migrate_users.py "$@"
