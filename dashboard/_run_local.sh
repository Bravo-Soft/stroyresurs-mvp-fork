#!/usr/bin/env bash
# Локальный запуск Диспетчерской без Docker. Выполняется из любого каталога.
set -euo pipefail

dashboard_dir="$(cd "$(dirname "$0")" && pwd)"
mvp_dir="$(cd "$dashboard_dir/.." && pwd)"

if [[ -f "$dashboard_dir/local.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$dashboard_dir/local.env"
  set +a
fi

: "${DASHBOARD_TOKEN:=test-token-local}"
export DASHBOARD_TOKEN

cd "$mvp_dir"
exec "${PYTHON_BIN:-python3}" -m uvicorn dashboard.app:app \
  --host "${DASHBOARD_HOST:-127.0.0.1}" --port "${DASHBOARD_PORT:-5510}"
