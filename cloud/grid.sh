#!/usr/bin/env bash
# Rent a box, run the grid on it, bring the results home, destroy the box.
#
#   ./cloud/grid.sh up        provision + install + upload + start the grid
#   ./cloud/grid.sh status    is it alive, how far in, what has it cost
#   ./cloud/grid.sh fetch     pull results back (safe to repeat)
#   ./cloud/grid.sh down      DESTROY. the only thing that stops billing
#   ./cloud/grid.sh cost      hours x rate, touching nothing
#
# WHY THIS EXISTS. The grid is ~20 hours of compute that does not fit on the workstation:
# one riskmatch_wf cell peaks at 9.6 GB, and the 2026-08-27 attempt took the machine down
# -- not by exhausting RAM, which stayed at 12 GB free, but by exhausting the COMMIT CHARGE
# when a deadlocked cell left orphaned workers holding 85 GB with a ZERO working set.
# Everything started afterwards died on allocation, including make_book_rules.py while
# merely importing pandas. Renting means that failure costs a machine nobody is using.
#
# TURNING IT OFF IS NOT A THING. Vultr and Hetzner both bill a powered-off instance at the
# full rate, because it still reserves CPU, RAM, disk and IP. Only DELETE stops the meter.
# Every failure path in "up" destroys the box (see the trap); if this script itself dies,
# the fallback is the curl command it prints before doing anything else.
#
# THE PAYLOAD IS 2.45 GB, NOT 9.6. The grid reads 1d/4h/1h/15m/5m and never touches the 1m
# cache -- 5m and 15m are already materialised as their own parquets -- so 5.5 GB of
# 1m/2m/3m stays home. Everything else arrives by git clone.
#
# NO VENDOR KEYS TRAVEL. The grid only reads the cache; nothing fetches.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
STATE="$ROOT/cloud/.state"
KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_stockhunt}"
PY="$ROOT/.venv/Scripts/python"

REGION="${REGION:-sgp}"
PLAN="${PLAN:-vc2-16c-64gb}"
OS_ID="${OS_ID:-2284}"
RATE_USD="${RATE_USD:-0.476}"   # $320/mo over the 672h vc2 cap, confirmed from /v2/plans
MAX_HOURS="${MAX_HOURS:-26}"

api() {
  local m="$1" p="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$m" "https://api.vultr.com/v2$p" \
      -H "Authorization: Bearer $VULTR_API_KEY" \
      -H "Content-Type: application/json" -d "$body"
  else
    curl -sS -X "$m" "https://api.vultr.com/v2$p" -H "Authorization: Bearer $VULTR_API_KEY"
  fi
}

need_key() {
  if [ -z "${VULTR_API_KEY:-}" ] && [ -f "$ROOT/.env.local" ]; then
    VULTR_API_KEY=$(grep -E "^VULTR_API_KEY=" "$ROOT/.env.local" | head -1 | cut -d= -f2- | tr -d '\r"')
  fi
  if [ -z "${VULTR_API_KEY:-}" ]; then
    echo "VULTR_API_KEY not set. Put it in .env.local or export it." >&2
    echo "Vultr also blocks API calls from un-whitelisted IPs:" >&2
    echo "  Account -> API -> Access Control -> add your public IP" >&2
    return 1
  fi
  export VULTR_API_KEY
}

cmd_cost() {
  [ -f "$STATE" ] || { echo "no box"; return 0; }
  . "$STATE"
  local mins=$(( ( $(date +%s) - CREATED_AT ) / 60 ))
  MINS="$mins" R="$RATE_USD" MH="$MAX_HOURS" "$PY" - <<'PYCOST'
import os
m = int(os.environ["MINS"]); r = float(os.environ["R"]); mh = float(os.environ["MH"])
print(f"up {m//60}h {m%60}m  ->  ${m/60*r:.2f} so far   (ceiling {mh:.0f}h = ${mh*r:.2f})")
PYCOST
}

cmd_down() {
  need_key || return 1
  [ -f "$STATE" ] || { echo "no box recorded -- nothing to destroy"; return 0; }
  . "$STATE"
  echo "destroying $INSTANCE_ID ..."
  api DELETE "/instances/$INSTANCE_ID" >/dev/null
  sleep 4
  # Verify. A delete that silently failed is a machine that bills all night.
  if api GET "/instances/$INSTANCE_ID" | grep -q "main_ip"; then
    echo "!!! STILL PRESENT. Destroy it by hand NOW:" >&2
    echo "    curl -X DELETE https://api.vultr.com/v2/instances/$INSTANCE_ID -H \"Authorization: Bearer \$VULTR_API_KEY\"" >&2
    return 1
  fi
  cmd_cost
  rm -f "$STATE"
  echo "DESTROYED -- billing stopped."
}

cmd_status() {
  [ -f "$STATE" ] || { echo "no box"; return 0; }
  . "$STATE"
  cmd_cost
  echo "--- remote log ---"
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@$IP" \
    "tail -30 /opt/stockhunt/cloud_grid.log 2>/dev/null || echo no-log-yet" 2>/dev/null \
    || echo "(unreachable)"
}

cmd_fetch() {
  [ -f "$STATE" ] || { echo "no box"; return 1; }
  . "$STATE"
  echo "pulling results ..."
  # Sheets only. The bars went UP; nothing but results comes DOWN.
  rsync -az --info=progress2 -e "ssh -i $KEY -o StrictHostKeyChecking=no" \
    --include="*/" --include="*.csv" --include="*.json" --exclude="*" \
    "root@$IP:/opt/stockhunt/wfo-results/" \
    "$ROOT/walk-forward optimization/results/"
  echo "landed in walk-forward optimization/results/"
}

json_field() {   # json_field <dotted.path>  -- reads stdin, "" on failure
  "$PY" "$ROOT/cloud/_jf.py" "$1"
}

cmd_up() {
  need_key || return 1
  [ -f "$STATE" ] && { echo "a box already exists (cloud/.state). Run down first."; return 1; }

  local kid
  kid=$(api GET /ssh-keys | json_field ssh_keys.0.id)
  if [ -z "$kid" ]; then
    echo "no SSH key on the Vultr account. Add this under Account -> SSH Keys:" >&2
    cat "$KEY.pub" >&2
    return 1
  fi

  echo "creating $PLAN in $REGION ..."
  local resp id
  resp=$(api POST /instances "{\"region\":\"$REGION\",\"plan\":\"$PLAN\",\"os_id\":$OS_ID,\"sshkey_id\":[\"$kid\"],\"label\":\"stockhunt-grid\",\"hostname\":\"stockhunt-grid\",\"backups\":\"disabled\",\"enable_ipv6\":false}")
  id=$(echo "$resp" | json_field instance.id)
  if [ -z "$id" ]; then echo "create failed:"; echo "$resp"; return 1; fi

  printf 'INSTANCE_ID=%s\nCREATED_AT=%s\nIP=\n' "$id" "$(date +%s)" > "$STATE"
  echo
  echo "BOX CREATED. If everything below fails, THIS destroys it:"
  echo "  curl -X DELETE https://api.vultr.com/v2/instances/$id -H \"Authorization: Bearer \$VULTR_API_KEY\""
  echo
  # Anything from here until the grid is running tears the box down rather than leaking it.
  trap 'echo; echo "provision failed -- tearing down"; cmd_down' ERR INT TERM

  echo "waiting for an IP ..."
  local ip=""
  for _ in $(seq 1 60); do
    ip=$(api GET "/instances/$id" | json_field instance.main_ip)
    [ -n "$ip" ] && [ "$ip" != "0.0.0.0" ] && break
    sleep 10
  done
  [ -n "$ip" ] && [ "$ip" != "0.0.0.0" ] || { echo "no IP after 10 minutes"; return 1; }
  sed -i "s/^IP=.*/IP=$ip/" "$STATE"
  echo "  ip $ip"

  echo "waiting for sshd ..."
  for _ in $(seq 1 40); do
    ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$ip" true 2>/dev/null && break
    sleep 10
  done

  echo "installing (TA-Lib C library, venv, deps) ..."
  ssh -i "$KEY" -o StrictHostKeyChecking=no "root@$ip" "bash -s" < cloud/remote/setup.sh

  echo "uploading the cache (~2.45 GB) ..."
  "$PY" cloud/payload_manifest.py > /tmp/sh_payload.txt || return 1
  rsync -az --info=progress2 -e "ssh -i $KEY -o StrictHostKeyChecking=no" \
    --files-from=/tmp/sh_payload.txt "$ROOT/" "root@$ip:/opt/stockhunt/"

  echo "starting the grid (detached: survives this shell and the SSH session) ..."
  scp -q -i "$KEY" -o StrictHostKeyChecking=no cloud/remote/run_grid.sh "root@$ip:/opt/stockhunt/"
  ssh -i "$KEY" -o StrictHostKeyChecking=no "root@$ip" \
    "cd /opt/stockhunt && chmod +x run_grid.sh && setsid nohup ./run_grid.sh > cloud_grid.log 2>&1 < /dev/null & sleep 2; echo started"

  trap - ERR INT TERM
  echo
  echo "RUNNING."
  echo "  ./cloud/grid.sh status   watch it"
  echo "  ./cloud/grid.sh fetch    pull results"
  echo "  ./cloud/grid.sh down     DESTROY -- the only thing that stops billing"
}


# A dry run of the whole lifecycle: create the REAL plan, wait for it, ssh in, destroy it,
# and verify it is gone. Costs a few cents and answers the only question that matters before
# a 20-hour run -- does the create/destroy path actually work on this account, with this key,
# from this IP, for this plan in this region.
#
# It uses the SAME api/need_key/json_field/cmd_down code the real run does, deliberately: a
# smoke test that exercises a separate path proves nothing about the path you will use.
cmd_test() {
  need_key || return 1
  [ -f "$STATE" ] && { echo "a box already exists (cloud/.state). Run down first."; return 1; }
  local kid resp id ip t0 ok=1
  t0=$(date +%s)

  kid=$(api GET /ssh-keys | json_field ssh_keys.0.id)
  [ -n "$kid" ] || { echo "FAIL: no SSH key on the account"; return 1; }
  echo "ssh key      OK  $kid"

  echo "creating $PLAN in $REGION ..."
  resp=$(api POST /instances "{\"region\":\"$REGION\",\"plan\":\"$PLAN\",\"os_id\":$OS_ID,\"sshkey_id\":[\"$kid\"],\"label\":\"stockhunt-smoketest\",\"hostname\":\"smoketest\",\"backups\":\"disabled\",\"enable_ipv6\":false}")
  id=$(echo "$resp" | json_field instance.id)
  [ -n "$id" ] || { echo "FAIL: create rejected:"; echo "$resp"; return 1; }
  printf 'INSTANCE_ID=%s
CREATED_AT=%s
IP=
' "$id" "$(date +%s)" > "$STATE"
  echo "create       OK  $id"
  echo "  emergency: curl -X DELETE https://api.vultr.com/v2/instances/$id -H \"Authorization: Bearer \$VULTR_API_KEY\""

  for _ in $(seq 1 60); do
    ip=$(api GET "/instances/$id" | json_field instance.main_ip)
    [ -n "$ip" ] && [ "$ip" != "0.0.0.0" ] && break
    sleep 10
  done
  if [ -n "$ip" ] && [ "$ip" != "0.0.0.0" ]; then
    sed -i "s/^IP=.*/IP=$ip/" "$STATE"
    echo "ip           OK  $ip  ($(( $(date +%s) - t0 ))s)"
  else
    echo "ip           FAIL (none after 10 min)"; ok=0
  fi

  if [ "$ok" = "1" ]; then
    local sshok=0
    for _ in $(seq 1 30); do
      if ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$ip"            "nproc; free -g | awk '/Mem:/ {print \$2\" GB\"}'" 2>/dev/null; then sshok=1; break; fi
      sleep 10
    done
    [ "$sshok" = "1" ] && echo "ssh          OK  ($(( $(date +%s) - t0 ))s from create)"                        || { echo "ssh          FAIL"; ok=0; }
  fi

  echo "destroying ..."
  if cmd_down; then echo "destroy      OK"; else echo "destroy      FAIL"; ok=0; fi

  echo
  [ "$ok" = "1" ] && echo "SMOKE TEST PASSED -- create, reach, destroy all work."                   || echo "SMOKE TEST FAILED -- see above. Check nothing is left running."
}

case "${1:-}" in
  up) cmd_up ;;
  test) cmd_test ;;
  status) cmd_status ;;
  fetch) cmd_fetch ;;
  down) cmd_down ;;
  cost) cmd_cost ;;
  *) sed -n "2,8p" "$0"; exit 1 ;;
esac
