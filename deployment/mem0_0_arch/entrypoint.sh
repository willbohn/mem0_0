#!/bin/sh
set -eu

test "${MEM0_0_DEPLOYMENT_MODE:-}" = "shadow"
test "${MEM0_0_API_PORT:-}" = "28889"

alembic upgrade head
exec uvicorn main:app --host 127.0.0.1 --port 28889
