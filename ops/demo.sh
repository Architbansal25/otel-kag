#!/usr/bin/env bash
# =============================================================================
# OTel + KAG demo control script -- runs inside WSL2.
#
# Why WSL: the Windows JVM on this machine cannot create NIO selector loopback
# pipes (endpoint security blocks the socket pair), so no Java server will start
# there. Inside WSL the same jars run normally, and WSL2 forwards listening ports
# to Windows localhost -- so the Python KAG engine, the browser and Jaeger's UI
# all still work from Windows exactly as if the services ran natively.
#
#   ./demo.sh deps               download Jaeger (Linux) -- once
#   ./demo.sh build              rebuild the service jars (needs Maven)
#   ./demo.sh start              start Jaeger + all four services (healthy)
#   ./demo.sh load [secs]        generate traffic
#   ./demo.sh status             what is up
#
#   Breaking things live (the audience never sees these):
#   ./demo.sh chaos latency <ms> slow down every inventory data fetch
#   ./demo.sh chaos errors <0-1> fail that fraction of inventory fetches
#   ./demo.sh chaos clear        remove injected latency / errors
#   ./demo.sh kill <svc>         stop one service (simulates a crash)
#   ./demo.sh restart <svc>      bring one service back
#   ./demo.sh heal               clear chaos and restart anything that is down
#
#   ./demo.sh scenario 1         poison message
#   ./demo.sh scenario 2         connection pool starvation (bad deploy)
#   ./demo.sh reset              back to healthy
#   ./demo.sh stop               stop everything
#   ./demo.sh logs <svc>         tail a service log
# =============================================================================
set -uo pipefail

# The repo root, wherever it is checked out (/mnt/c/... under WSL, or anywhere
# on Linux/macOS). Override with DEMO_ROOT if you really need to.
WIN_ROOT="${DEMO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Jars are copied out of /mnt/c into the WSL filesystem: starting from /mnt/c
# takes ~22s per service, from ext4 it is ~4s. That matters when you redeploy
# inventory-svc live in front of the room.
RUN_DIR="$HOME/otel-kag-demo"
LOG_DIR="$RUN_DIR/logs"
AGENT="$RUN_DIR/opentelemetry-javaagent.jar"
JAEGER="$RUN_DIR/jaeger-all-in-one"
DEPLOYS="$WIN_ROOT/kag/deploys.json"

mkdir -p "$RUN_DIR" "$LOG_DIR"

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }

# --- helpers ---------------------------------------------------------------
sync_jars() {
  rsync -a --delete-after \
      "$WIN_ROOT/services/broker/target/broker-1.0.0.jar" \
      "$WIN_ROOT/services/inventory-svc/target/inventory-svc-1.0.0.jar" \
      "$WIN_ROOT/services/order-api/target/order-api-1.0.0.jar" \
      "$WIN_ROOT/services/notification-svc/target/notification-svc-1.0.0.jar" \
      "$RUN_DIR/" 2>/dev/null || {
    for j in broker inventory-svc order-api notification-svc; do
      cp -f "$WIN_ROOT/services/$j/target/$j-1.0.0.jar" "$RUN_DIR/" || return 1
    done
  }
  [ -f "$AGENT" ] || cp -f "$WIN_ROOT/vendor/opentelemetry-javaagent.jar" "$AGENT"
}

wait_health() {
  local name=$1 port=$2 timeout=${3:-90} i
  local body
  for ((i = 0; i < timeout; i++)); do
    # Started = the health endpoint answers. It may answer DOWN (503) when a
    # dependency is missing -- e.g. the broker is the thing that was killed --
    # and waiting for UP would then hang for the full timeout.
    body=$(curl -s --max-time 2 "http://localhost:$port/actuator/health" 2>/dev/null)
    if printf '%s' "$body" | grep -q '"status":"UP"'; then
      green "  [up]   $name"
      return 0
    elif printf '%s' "$body" | grep -q '"status"'; then
      yellow "  [up]   $name (running, but a dependency is DOWN)"
      return 0
    fi
    sleep 1
  done
  red "  [slow] $name not healthy after ${timeout}s -- see $LOG_DIR/$name.log"
  return 1
}

start_svc() {
  local name=$1 host=$2
  shift 2
  nohup java \
    -javaagent:"$AGENT" \
    -Dotel.service.name="$name" \
    -Dotel.traces.exporter=otlp \
    -Dotel.metrics.exporter=none \
    -Dotel.logs.exporter=none \
    -Dotel.exporter.otlp.protocol=grpc \
    -Dotel.exporter.otlp.endpoint=http://localhost:4317 \
    -Dotel.traces.sampler=always_on \
    -Dotel.resource.attributes=host.name="$host",deployment.environment=demo \
    "$@" \
    -jar "$RUN_DIR/$name-1.0.0.jar" \
    > "$LOG_DIR/$name.log" 2>&1 &
  echo "  [..]   $name starting (host=$host)"
}

# name -> "host port"
svc_info() {
  case "$1" in
    broker)           echo "host-b 8084" ;;
    inventory-svc)    echo "host-b 8082" ;;
    order-api)        echo "host-a 8081" ;;
    notification-svc) echo "host-a 8083" ;;
    *) return 1 ;;
  esac
}

is_up() {
  curl -sf --max-time 2 "http://localhost:$1/actuator/health" >/dev/null 2>&1
}

record_deploy() {
  # record_deploy <id> <target> <by> <note> [key] [from] [to] [configures] [--reset]
  local id=$1 target=$2 by=$3 note=$4 key=${5:-} from=${6:-} to=${7:-} configures=${8:-} reset=${9:-}
  DEPLOYS="$DEPLOYS" ID="$id" TARGET="$target" BY="$by" NOTE="$note" \
  KEY="$key" FROM="$from" TO="$to" CONFIGURES="$configures" RESET="$reset" \
  AGE_MIN="${AGE_MIN:-0}" \
  python3 - <<'PY'
import json, os, datetime
path = os.environ["DEPLOYS"]
log = []
if os.path.exists(path) and os.environ.get("RESET") != "--reset":
    try:
        log = json.load(open(path, encoding="utf-8")) or []
    except (ValueError, OSError):
        log = []
changes = []
if os.environ.get("KEY"):
    changes = [{"key": os.environ["KEY"], "from": os.environ["FROM"],
                "to": os.environ["TO"], "configures": os.environ["CONFIGURES"]}]
age = datetime.timedelta(minutes=float(os.environ.get("AGE_MIN") or 0))
log.append({
    "id": os.environ["ID"],
    "at": (datetime.datetime.now() - age).strftime("%Y-%m-%dT%H:%M:%S"),
    "by": os.environ["BY"],
    "note": os.environ["NOTE"],
    "target": os.environ["TARGET"],
    "changes": changes,
})
with open(path, "w", encoding="utf-8") as fh:
    json.dump(log, fh, indent=2)
print("  [log]  deploy #%s -> %s  %s" % (os.environ["ID"], os.environ["TARGET"], os.environ["NOTE"]))
PY
}

start_inventory() {
  local pool=$1
  start_svc inventory-svc host-b -DINVENTORY_POOL_SIZE="$pool" -DINVENTORY_QUERY_HOLD_MS=150
  wait_health inventory-svc 8082
}

# --- commands --------------------------------------------------------------
cmd_deps() {
  if [ -f "$JAEGER" ]; then green "Jaeger already present"; return; fi
  echo "Resolving latest Jaeger 1.x release..."
  local url
  url=$(curl -s "https://api.github.com/repos/jaegertracing/jaeger/releases?per_page=60" \
        | grep -o 'https://[^"]*jaeger-1\.[0-9.]*-linux-amd64\.tar\.gz' | head -1)
  # The releases API is rate-limited and often blocked on corporate networks;
  # fall back to a known-good pinned release rather than failing.
  [ -n "$url" ] || url="https://github.com/jaegertracing/jaeger/releases/download/v1.62.0/jaeger-1.62.0-linux-amd64.tar.gz"
  echo "Downloading $(basename "$url")"
  curl -sL "$url" -o /tmp/jaeger.tar.gz || { red "download failed"; return 1; }
  tar -xzf /tmp/jaeger.tar.gz -C "$RUN_DIR" --strip-components=1 \
      --wildcards '*/jaeger-all-in-one' && chmod +x "$JAEGER"
  rm -f /tmp/jaeger.tar.gz
  [ -f "$JAEGER" ] && green "Jaeger ready at $JAEGER" || red "extraction failed"
}

cmd_start() {
  local pool=${1:-20}
  echo ""
  yellow "=== OTel + KAG demo (WSL runtime) ==="
  echo ""
  [ -f "$JAEGER" ] || { red "Jaeger missing. Run: ./demo.sh deps"; return 1; }

  echo "  [..]   syncing jars into the WSL filesystem"
  sync_jars || { red "jar sync failed -- did 'mvn package' run?"; return 1; }

  # Test the PORT, not the process. `pgrep` returns true for a Jaeger that is
  # still shutting down, so a stop immediately followed by a start used to
  # "[skip]" a Jaeger that then finished dying -- leaving the whole demo running
  # with no trace backend, no error symptom, and no visible error anywhere.
  if curl -sf --max-time 2 http://localhost:16686/api/services >/dev/null 2>&1; then
    echo "  [skip] Jaeger already up"
  else
    pkill -f '[j]aeger-all-in-one' >/dev/null 2>&1
    sleep 1
    COLLECTOR_OTLP_ENABLED=true nohup "$JAEGER" > "$LOG_DIR/jaeger.log" 2>&1 &
    echo "  [..]   Jaeger starting (UI :16686, OTLP :4317)"
    local i
    for ((i = 0; i < 40; i++)); do
      sleep 1
      if curl -sf --max-time 2 http://localhost:16686/api/services >/dev/null 2>&1; then
        green "  [up]   jaeger"
        break
      fi
    done
    if ! curl -sf --max-time 2 http://localhost:16686/api/services >/dev/null 2>&1; then
      red "  [FAIL] Jaeger never came up -- see $LOG_DIR/jaeger.log"
      return 1
    fi
  fi

  start_svc broker host-b
  wait_health broker 8084
  start_inventory "$pool"
  start_svc order-api host-a
  start_svc notification-svc host-a
  wait_health order-api 8081
  wait_health notification-svc 8083

  # Backdated: a baseline release is days old, not seconds. Without this the
  # recency term cannot tell the original release from the change that broke it.
  AGE_MIN=4320 record_deploy 44 inventory-svc ci-pipeline "baseline release" \
      "spring.datasource.hikari.maximum-pool-size" "" "$pool" "inventory-pool" --reset

  echo ""
  green "Ready. From Windows these are all on localhost:"
  echo "  Jaeger UI        http://localhost:16686"
  echo "  order-api        http://localhost:8081/swagger-ui.html"
  echo "  inventory-svc    http://localhost:8082/swagger-ui.html"
  echo "  health           http://localhost:8081/actuator/health"
  echo "  inventory pool   http://localhost:8082/admin/pool"
  echo "  consumer stats   http://localhost:8083/admin/stats"
  echo ""
  yellow "Next:  ./demo.sh load 300"
  echo ""
}

cmd_load() {
  local secs=${1:-300} workers=${2:-8}
  echo ""
  yellow "Load: $workers workers for ${secs}s"
  local deadline=$(( $(date +%s) + secs ))
  local skus=(SKU-1001 SKU-1002 SKU-1003)
  rm -f /tmp/kagload.*
  for ((w = 0; w < workers; w++)); do
    (
      ok=0; to=0; err=0
      while [ "$(date +%s)" -lt "$deadline" ]; do
        sku=${skus[$((RANDOM % 3))]}
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
               -X POST http://localhost:8081/orders \
               -H 'Content-Type: application/json' \
               -d "{\"sku\":\"$sku\"}" 2>/dev/null)
        case "$code" in
          201|200) ok=$((ok+1)) ;;
          504)     to=$((to+1)) ;;
          *)       err=$((err+1)) ;;
        esac
        sleep 0.12
      done
      echo "$ok $to $err" > "/tmp/kagload.$w"
    ) &
  done
  wait
  local ok=0 to=0 err=0
  for f in /tmp/kagload.*; do
    read -r a b c < "$f"; ok=$((ok+a)); to=$((to+b)); err=$((err+c))
  done
  local total=$((ok+to+err))
  echo ""
  green "Done. $total requests"
  echo "  placed         $ok"
  echo "  504 timeouts   $to"
  echo "  other errors   $err"
  [ "$total" -gt 0 ] && echo "  error rate     $(( 100*(to+err)/total ))%"
  echo ""
}

cmd_scenario() {
  case "${1:-}" in
    1)
      echo ""
      yellow "SCENARIO 1 - poison message"
      echo "  Malformed payload onto order.events. Single hop, loud, self-contained."
      echo "  The flat-log baseline solves this one too -- that is the point."
      echo ""
      for i in 1 2 3 4 5; do
        curl -s -X POST http://localhost:8081/orders/poison >/dev/null
        sleep 0.3
      done
      green "  Sent 5 malformed messages.  ./demo.sh logs notification-svc"
      ;;
    2)
      echo ""
      yellow "SCENARIO 2 - connection pool starvation"
      echo "  Redeploying inventory-svc with the pool cut from 20 to 2."
      echo ""
      # The decoy: recent, real, and completely irrelevant. It exists so the
      # ranking has to earn its answer instead of just picking the latest change.
      # The decoy configures a real service, so it IS a legitimate candidate with
      # a genuine path to both symptoms. It loses on evidence, not on topology --
      # which is the whole point of the mechanism term.
      AGE_MIN=25 record_deploy 46 notification-svc platform-team "raise consumer log level to DEBUG" \
          "logging.level.com.nagarro.demo.notification" "INFO" "DEBUG" "notification-svc"
      pkill -f inventory-svc-1.0.0.jar >/dev/null 2>&1
      sleep 2
      # Pool of 1, not 2.
      #
      # At 2 the demo is unreliable: service rate is ~13/s against 20 workers, so
      # the wait lands right on order-api's 2s timeout, and the system then
      # SELF-BALANCES -- failed orders publish no messages, the consumer idles,
      # the pool frees up, and requests start succeeding again. The error rate
      # oscillates around the threshold (57% one run, 10% the next) and the
      # symptom fires only sometimes. At 1 the service rate is ~6.6/s, the wait
      # is ~3s for 20 workers, and no amount of consumer idling can rescue it.
      record_deploy 47 inventory-svc release-bot "tune JDBC pool for cost savings" \
          "spring.datasource.hikari.maximum-pool-size" "20" "2" "inventory-pool"
      start_inventory 2
      echo ""
      green "  inventory-svc is back up with maximum-pool-size=2."
      echo "  Change log now holds #46 (decoy) and #47 (real)."
      echo ""
      yellow "  Now:  ./demo.sh load 120     then on Windows:  cd kag ; py rca.py"
      ;;
    *)
      red "Usage: ./demo.sh scenario [1|2]" ;;
  esac
  echo ""
}

cmd_reset() {
  echo ""
  yellow "Resetting to healthy (pool = 20)"
  pkill -f inventory-svc-1.0.0.jar >/dev/null 2>&1
  sleep 2
  AGE_MIN=4320 record_deploy 44 inventory-svc ci-pipeline "baseline release" \
      "spring.datasource.hikari.maximum-pool-size" "" "20" "inventory-pool" --reset
  start_inventory 20
  green "  Healthy."
  echo ""
}

cmd_status() {
  echo ""
  printf '%-18s %-6s %s\n' SERVICE PORT STATUS
  printf '%-18s %-6s %s\n' ------- ---- ------
  for pair in "jaeger 16686" "order-api 8081" "inventory-svc 8082" \
              "notification-svc 8083" "broker 8084"; do
    set -- $pair
    if curl -sf --max-time 2 "http://localhost:$2" >/dev/null 2>&1 \
       || curl -sf --max-time 2 "http://localhost:$2/actuator/health" 2>/dev/null | grep -q UP; then
      printf '%-18s %-6s \033[0;32mup\033[0m\n' "$1" "$2"
    else
      printf '%-18s %-6s \033[0;31mdown\033[0m\n' "$1" "$2"
    fi
  done
  echo ""
  echo "pool:  $(curl -s --max-time 2 http://localhost:8082/admin/pool || echo n/a)"
  echo "queue: $(curl -s --max-time 2 http://localhost:8083/admin/stats || echo n/a)"
  echo "chaos: $(curl -s --max-time 2 http://localhost:8082/admin/chaos || echo n/a)"
  echo ""
}

cmd_stop() {
  echo ""
  for j in broker inventory-svc order-api notification-svc; do
    pkill -f "$j-1.0.0.jar" >/dev/null 2>&1 && echo "  [stopped] $j"
  done
  pkill -f '[j]aeger-all-in-one' >/dev/null 2>&1 && echo "  [stopped] jaeger"

  # Wait for them to actually exit. Returning while processes are still dying
  # makes an immediately-following `start` misread the old process as healthy.
  local i
  for ((i = 0; i < 20; i++)); do
    if ! pgrep -f '[j]aeger-all-in-one' >/dev/null 2>&1 \
       && ! pgrep -f '[-]1.0.0.jar' >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
  echo ""
  green "All stopped."
  echo ""
}

cmd_build() {
  command -v mvn >/dev/null 2>&1 || { red "Maven (mvn) not found"; return 1; }
  (cd "$WIN_ROOT/services" && mvn -q -B package -DskipTests) \
    && green "Jars rebuilt under services/*/target" || { red "build failed"; return 1; }
}

cmd_chaos() {
  local what=${1:-} value=${2:-}
  is_up 8082 || { red "inventory-svc is not running"; return 1; }
  case "$what" in
    latency)
      [ -n "$value" ] || { red "Usage: ./demo.sh chaos latency <ms>"; return 1; }
      curl -s -X POST "http://localhost:8082/admin/chaos/latency?ms=$value"; echo
      yellow "  inventory-svc data fetches now take +${value} ms" ;;
    errors)
      [ -n "$value" ] || { red "Usage: ./demo.sh chaos errors <0-1>"; return 1; }
      curl -s -X POST "http://localhost:8082/admin/chaos/errors?rate=$value"; echo
      yellow "  inventory-svc now fails ${value} of data fetches" ;;
    clear)
      curl -s -X DELETE "http://localhost:8082/admin/chaos"; echo
      green "  chaos cleared" ;;
    *)
      red "Usage: ./demo.sh chaos [latency <ms>|errors <0-1>|clear]"; return 1 ;;
  esac
}

cmd_kill() {
  local svc=${1:-}
  svc_info "$svc" >/dev/null || { red "Usage: ./demo.sh kill <broker|inventory-svc|order-api|notification-svc>"; return 1; }
  if pkill -f "$svc-1.0.0.jar" >/dev/null 2>&1; then
    red "  [killed] $svc"
  else
    yellow "  $svc was not running"
  fi
}

cmd_restart() {
  local svc=${1:-} info host port
  info=$(svc_info "$svc") || { red "Usage: ./demo.sh restart <broker|inventory-svc|order-api|notification-svc>"; return 1; }
  read -r host port <<< "$info"
  pkill -f "$svc-1.0.0.jar" >/dev/null 2>&1 && sleep 2
  [ -f "$RUN_DIR/$svc-1.0.0.jar" ] || sync_jars
  if [ "$svc" = inventory-svc ]; then
    start_inventory 20
  else
    start_svc "$svc" "$host"
    wait_health "$svc" "$port"
  fi
}

cmd_heal() {
  echo ""
  yellow "Healing: clearing chaos and restarting anything that is down"
  local svc info host port
  for svc in broker inventory-svc order-api notification-svc; do
    read -r host port <<< "$(svc_info "$svc")"
    if ! is_up "$port"; then
      cmd_restart "$svc"
    fi
  done
  is_up 8082 && curl -s -X DELETE "http://localhost:8082/admin/chaos" >/dev/null && green "  chaos cleared"
  green "  Healthy."
  echo ""
}

cmd_logs() {
  local svc=${1:-}
  [ -n "$svc" ] || { red "Usage: ./demo.sh logs <broker|inventory-svc|order-api|notification-svc|jaeger>"; return 1; }
  tail -f "$LOG_DIR/$svc.log"
}

case "${1:-}" in
  deps)     cmd_deps ;;
  build)    cmd_build ;;
  chaos)    cmd_chaos "${2:-}" "${3:-}" ;;
  kill)     cmd_kill "${2:-}" ;;
  restart)  cmd_restart "${2:-}" ;;
  heal)     cmd_heal ;;
  start)    cmd_start "${2:-20}" ;;
  load)     cmd_load "${2:-300}" "${3:-8}" ;;
  scenario) cmd_scenario "${2:-}" ;;
  reset)    cmd_reset ;;
  status)   cmd_status ;;
  stop)     cmd_stop ;;
  logs)     cmd_logs "${2:-}" ;;
  *)
    sed -n '3,34p' "$0" | sed 's/^# \?//'
    ;;
esac
