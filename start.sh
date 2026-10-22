#!/usr/bin/env bash
# Bring the pilot up with one command.
#
# Written for the case where typing is expensive -- an SSH session from a
# tablet, a fresh box you just cloned onto. Every step is skipped if it has
# already been done, so re-running it is cheap and safe.
#
#   ./start.sh            bind to localhost  (use with an SSH tunnel)
#   ./start.sh --public   bind to 0.0.0.0    (Codespaces, or a LAN machine)
#
set -euo pipefail
cd "$(dirname "$0")"

HOST=127.0.0.1
for arg in "$@"; do
  case "$arg" in
    --public) HOST=0.0.0.0 ;;
    # Print the header comment, stopping at the first line of actual code,
    # so help and documentation cannot drift apart.
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
PORT="${PORT:-8000}"

PY=python3
command -v "$PY" >/dev/null || { echo "python3 not found" >&2; exit 1; }

# 3.11 is the floor: the domain layer uses enum.StrEnum.
"$PY" - <<'PYEOF' || { echo "Python 3.11+ required; found $($PY -V)" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PYEOF

if [ ! -d .venv ]; then
  echo "==> creating .venv"
  "$PY" -m venv .venv
fi

if ! .venv/bin/python -c "import fastapi, h3" >/dev/null 2>&1; then
  echo "==> installing dependencies"
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f medic.sqlite3 ]; then
  echo "==> seeding the Jaipur pilot data"
  .venv/bin/python -m medic.seed
fi

echo
echo "    customer  http://${HOST}:${PORT}/customer/"
echo "    chemist   http://${HOST}:${PORT}/chemist/"
echo "    courier   http://${HOST}:${PORT}/courier/"
echo "    admin     http://${HOST}:${PORT}/admin/"
echo
echo "    Dispatch loop runs in this process. Ctrl-C stops everything."
echo

# MEDIC_RUN_WORKER is the point of this script: without the dispatch loop,
# offers never expire and every order stalls after its first one.
exec env MEDIC_RUN_WORKER=1 .venv/bin/python -m uvicorn medic.main:app \
  --host "$HOST" --port "$PORT"
