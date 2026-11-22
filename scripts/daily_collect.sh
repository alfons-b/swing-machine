#!/usr/bin/env bash
# Daily data collection for the cloud session (owner request 2026-11-21, until 2026-11-29).
# Exit code: 0 healthy, 1 degraded, 2 failed, 3 environment problem (no DB / no token).
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="logs/daily-$(date -u +%Y%m%d).log"
mkdir -p logs
{
  echo "== $(date -u '+%Y-%m-%d %H:%M UTC') daily collection"
  service postgresql status >/dev/null 2>&1 || service postgresql start >/dev/null 2>&1
  for _ in $(seq 1 15); do pg_isready -q && break; sleep 2; done
  if ! pg_isready -q; then echo "PostgreSQL is not running"; exit 3; fi
  if [ ! -f .env ] || ! grep -q '^IDX_EODHD_API_TOKEN=.\+' .env; then
    echo "MISSING .env with IDX_EODHD_API_TOKEN (container was reset?)"; exit 3
  fi
  uv sync -q --locked
  uv run idx db upgrade
  uv run idx daily; rc=$?
  uv run idx fin fetch-bca || echo "fetch-bca: no new report or source unavailable"
  echo "== idx daily exit code: $rc"
  exit "$rc"
} >>"$LOG" 2>&1
