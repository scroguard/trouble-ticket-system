#!/usr/bin/env bash
# Run the automated test suites against a throwaway copy of the system.
#
#   tests/run.sh                 # every suite
#   tests/run.sh api ui_portal   # just these
#
# Each suite gets a fresh stack under its own Docker Compose project (default
# "tickets-test") and its own ports, with the bundled GreenMail test mail server.
# It never touches your real deployment, its database or its .env settings.
set -uo pipefail
cd "$(dirname "$0")/.."

export COMPOSE_PROJECT_NAME="${TT_PROJECT:-tickets-test}"
case "$COMPOSE_PROJECT_NAME" in
  *-test) ;;
  *) echo "Refusing to run: TT_PROJECT must end in '-test' (the suites delete their project's data)." >&2; exit 2 ;;
esac

export TT_WEB_PORT="${TT_WEB_PORT:-18000}"
export MAILTEST_SMTP_PORT="${TT_SMTP_PORT:-13025}"
export MAILTEST_IMAP_PORT="${TT_IMAP_PORT:-13143}"
export WEB_PORT="$TT_WEB_PORT"
export TT_BASE_URL="http://localhost:$TT_WEB_PORT" TT_SMTP_PORT="$MAILTEST_SMTP_PORT" TT_IMAP_PORT="$MAILTEST_IMAP_PORT"
export POSTGRES_DB=tickets POSTGRES_USER=tickets POSTGRES_PASSWORD=tickets-test
PLAYWRIGHT_IMAGE="mcr.microsoft.com/playwright/python:v1.49.1-noble"
SHOTS="$PWD/tests/.shots"; LOGS="$PWD/tests/.logs"
mkdir -p "$SHOTS" "$LOGS"

for tool in docker python3; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 2; }
done
python3 -c "import requests" 2>/dev/null || { echo "Python package 'requests' is required (pip install requests)" >&2; exit 2; }
# docker-compose.yml reads .env; the test settings override it, but it has to exist.
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example (compose needs one; tests override its values)."; }

compose() { docker compose -f docker-compose.yml -f tests/compose.test.yml $EXTRA --profile mailtest "$@"; }

fresh() {  # fresh stack with optional extra override file
  EXTRA="${1:+-f $1}"
  export TT_COMPOSE_FILES="-f docker-compose.yml -f tests/compose.test.yml $EXTRA"
  compose down -v --remove-orphans >/dev/null 2>&1
  compose up -d --build >/dev/null 2>&1 || { echo "  could not start the test stack"; return 1; }
  for _ in $(seq 1 90); do curl -sf "$TT_BASE_URL/healthz" >/dev/null && return 0; sleep 2; done
  echo "  test stack did not become healthy"; return 1
}

seed() { python3 tests/seed.py >/dev/null; }

browser() {  # run a Playwright suite in the official Playwright image
  docker run --rm --network host --ipc=host \
    -e TT_BASE_URL -e TT_SMTP_PORT -e TT_IMAP_PORT -e TT_SHOTS=/out -e PYTHONPATH=/work \
    -v "$PWD/tests:/work:ro" -v "$SHOTS:/out" -w /work "$PLAYWRIGHT_IMAGE" \
    sh -c "pip install -q --break-system-packages playwright==1.49.1 requests >/dev/null 2>&1; python /work/$1"
}

run_suite() {
  case "$1" in
    api)           fresh && python3 tests/api_basic.py ;;
    api_admin)     fresh tests/compose.fast-retry.yml && python3 tests/api_admin.py ;;
    ingestion)     fresh && for u in "admin@example.com Ada admin" "alice@example.com Alice agent" "bob@example.com Bob agent"; do
                     set -- $u; compose run --rm --no-deps -T web python -m app.cli create-user --email "$1" --name "$2" --role "$3" --password supersecret1 >/dev/null 2>&1
                   done
                   compose run --rm --no-deps -T -v "$PWD/tests/ingestion_e2e.py:/app/e2e.py:ro" web python e2e.py ;;
    portal_api)    fresh tests/compose.portal-limits.yml && python3 tests/portal_api.py ;;
    reply_alerts)  fresh && python3 tests/reply_alerts.py ;;
    loop)          fresh && python3 tests/loop_protection.py ;;
    ui_dashboard)  fresh && seed && browser ui_dashboard.py ;;
    ui_admin)      fresh && seed && browser ui_admin.py ;;
    ui_site_name)  fresh && seed && browser ui_site_name.py ;;
    ui_signature)  fresh && seed && browser ui_signature.py ;;
    ui_portal)     fresh && browser ui_portal.py ;;
    *) echo "unknown suite: $1"; return 2 ;;
  esac
}

ALL=(api api_admin ingestion portal_api reply_alerts loop ui_dashboard ui_admin ui_site_name ui_signature ui_portal)
if [ $# -gt 0 ]; then SUITES=("$@"); else SUITES=("${ALL[@]}"); fi
failed=()
for suite in "${SUITES[@]}"; do
  printf '%-14s ' "$suite"
  EXTRA=""
  run_suite "$suite" >"$LOGS/$suite.log" 2>&1; status=$?
  passes=$(grep -c '^PASS' "$LOGS/$suite.log"); fails=$(grep -c '^FAIL' "$LOGS/$suite.log")
  if [ $status -eq 0 ]; then echo "ok    ($passes checks)"; else echo "FAILED ($passes passed, $fails failed; see tests/.logs/$suite.log)"; failed+=("$suite"); fi
done
EXTRA=""; compose down -v --remove-orphans >/dev/null 2>&1

echo
if [ ${#failed[@]} -eq 0 ]; then echo "All suites passed."; else echo "Failed: ${failed[*]}"; exit 1; fi
