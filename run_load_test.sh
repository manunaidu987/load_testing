#!/usr/bin/env bash
# =============================================================================
#  Load Test Runner — Log Optimization Dashboard API
#  Default host : http://localhost:8001
#  Default run  : 20 users | ramp-up 2/sec | 10 minutes
# =============================================================================
#  Commands:
#    ./run_load_test.sh          Run default (20 users, ramp 2, 10 min)
#    ./run_load_test.sh default  Same as above (explicit)
#    ./run_load_test.sh smoke    1 user — every endpoint once (CI check)
#    ./run_load_test.sh medium   100 users, ramp 10, 10 min
#    ./run_load_test.sh stress   300 users, ramp 20, 10 min
#    ./run_load_test.sh ui       Interactive web UI on :8089
#    ./run_load_test.sh all      smoke → default → medium in sequence
#
#  Override host:
#    API_HOST=https://staging.example.com ./run_load_test.sh
# =============================================================================
set -e

HOST="${API_HOST:-http://localhost:8001}"
OUT="./load_test_results"
mkdir -p "$OUT"

# ── shared settings (edit these to change the default run) ───────────────────
USERS=20          # total virtual users
RAMP=2            # users spawned per second
DURATION=10m      # how long to sustain load

banner() {
  echo ""
  echo "══════════════════════════════════════════════════"
  echo "  $*"
  echo "══════════════════════════════════════════════════"
}

check_locust() {
  command -v locust &>/dev/null || {
    echo "Locust not found. Install with:  pip install locust"
    exit 1
  }
}

# ── default run: 20 users | ramp 2 | 10 min ──────────────────────────────────
load_test_default() {
  banner "DEFAULT LOAD — ${USERS} users | ramp ${RAMP}/sec | ${DURATION}"
  echo "  Host  : $HOST"
  echo "  Roles : Admin (60%) | Privileged (25%) | Regular (15%)"
  echo "  Goal  : 500+ samples per endpoint for reliable p95/p99"
  echo ""
  locust -f locustfile.py --host="$HOST" \
    --headless --only-summary \
    -u "$USERS" -r "$RAMP" --run-time "$DURATION" \
    --html  "$OUT/default_report.html" \
    --csv   "$OUT/default" \
    AdminUser PrivilegedUser RegularUser \
    2>&1 | tee "$OUT/default.log"
  echo ""
  echo "  ✓ Report saved → $OUT/default_report.html"
  echo "  ✓ CSV data     → $OUT/default_stats.csv"
}

# ── smoke: 1 user, one pass through every endpoint ───────────────────────────
smoke_test() {
  banner "SMOKE TEST — 1 user | 90s"
  echo "  Host: $HOST"
  locust -f locustfile.py --host="$HOST" \
    --headless --only-summary \
    -u 1 -r 1 --run-time 90s \
    --html "$OUT/smoke_report.html" \
    --csv  "$OUT/smoke" \
    SmokeTestUser \
    2>&1 | tee "$OUT/smoke.log"
  echo "  ✓ Report → $OUT/smoke_report.html"
}

# ── medium: 100 users, 10 min ─────────────────────────────────────────────────
load_test_medium() {
  banner "MEDIUM LOAD — 100 users | ramp 10/sec | 10 min"
  locust -f locustfile.py --host="$HOST" \
    --headless --only-summary \
    -u 100 -r 10 --run-time 10m \
    --html "$OUT/medium_report.html" \
    --csv  "$OUT/medium" \
    AdminUser PrivilegedUser RegularUser \
    2>&1 | tee "$OUT/medium.log"
  echo "  ✓ Report → $OUT/medium_report.html"
}

# ── stress: 300 users, 10 min ────────────────────────────────────────────────
load_test_stress() {
  banner "STRESS TEST — 300 users | ramp 20/sec | 10 min"
  locust -f locustfile.py --host="$HOST" \
    --headless --only-summary \
    -u 300 -r 20 --run-time 10m \
    --html "$OUT/stress_report.html" \
    --csv  "$OUT/stress" \
    AdminUser PrivilegedUser RegularUser \
    2>&1 | tee "$OUT/stress.log"
  echo "  ✓ Report → $OUT/stress_report.html"
}

# ── web UI ───────────────────────────────────────────────────────────────────
web_ui() {
  banner "Web UI — http://localhost:8089"
  echo "  Set host to: $HOST in the UI"
  echo "  Suggested settings: users=20, spawn rate=2"
  locust -f locustfile.py --host="$HOST" AdminUser PrivilegedUser RegularUser
}

# ── dispatch ─────────────────────────────────────────────────────────────────
check_locust

case "${1:-default}" in
  default|"") load_test_default ;;
  smoke)       smoke_test ;;
  medium)      load_test_medium ;;
  stress)      load_test_stress ;;
  ui)          web_ui ;;
  all)
    smoke_test
    load_test_default
    load_test_medium
    ;;
  *)
    echo ""
    echo "Usage: $0 [default|smoke|medium|stress|ui|all]"
    echo ""
    echo "  default   20 users, ramp 2/sec, 10 min  ← recommended starting point"
    echo "  smoke     1 user, 90s — validates every endpoint"
    echo "  medium    100 users, ramp 10/sec, 10 min"
    echo "  stress    300 users, ramp 20/sec, 10 min"
    echo "  ui        Locust web UI at http://localhost:8089"
    echo "  all       smoke → default → medium in sequence"
    echo ""
    echo "  Override host:  API_HOST=https://staging.example.com $0"
    ;;
esac
