#!/usr/bin/env bash
# Five small boxes, one per asset class, instead of one big one.
#
#   ./cloud/fleet.sh up       provision + install + upload + start, all five in parallel
#   ./cloud/fleet.sh status   who is alive, how far in, what has it cost
#   ./cloud/fleet.sh reap     fetch + DESTROY every box that has finished (safe to repeat)
#   ./cloud/fleet.sh watch    reap on a loop until the fleet is empty
#   ./cloud/fleet.sh down     DESTROY everything now, finished or not
#   ./cloud/fleet.sh cost     what it has cost so far
#
# WHY A FLEET. The account caps the monthly fee of a SINGLE instance at ~$96, not the account
# total: six $20 boxes were accepted while one $160 box was refused. Buying width rather than
# size sidesteps that entirely -- and it happens to suit the work. riskmatch_wf must run one
# class at a time regardless (it judges "scoped" from --class and NEVER from --tf, so an
# all-class run rewrites edge_standard.csv whole), and portfolio_wf is single-threaded per
# cell. So five boxes finish in the time of the SLOWEST class instead of the sum of five.
#
# It is also cheaper twice over: the per-class payload means us_stocks uploads 1.6 GB while
# the other four upload 0.09-0.34 GB and start almost immediately, and `reap` destroys each
# box the moment ITS class is done rather than when the last one is.
#
# DESTROY IS VERIFIED, NOT ASSUMED, and that is not caution for its own sake. During the
# limit probe a DELETE returned success for six instances and all six were still running on
# the very next query; they only died on a second delete twelve seconds later. Reporting
# "destroyed" from the API response alone would have left six boxes billing overnight.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
FLEET="$ROOT/cloud/.fleet"
KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_stockhunt}"
PY="$ROOT/.venv/Scripts/python"
JF="$ROOT/cloud/_jf.py"

CLASSES="${CLASSES:-us_stocks cme_futures crypto us_etfs commodities}"
REGION="${REGION:-sgp}"
PLAN="${PLAN:-vhp-8c-16gb-amd}"   # 8 vCPU / 16 GB, $96/mo = $0.143/hr -- the largest the
                                  # account will deploy. Confirmed by probing the limit.
OS_ID="${OS_ID:-2284}"            # Ubuntu 24.04 LTS x64
RATE_USD="${RATE_USD:-0.143}"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

api() {
  local m="$1" p="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS --max-time 40 -X "$m" "https://api.vultr.com/v2$p" \
      -H "Authorization: Bearer $VULTR_API_KEY" -H "Content-Type: application/json" -d "$body"
  else
    curl -sS --max-time 40 -X "$m" "https://api.vultr.com/v2$p" -H "Authorization: Bearer $VULTR_API_KEY"
  fi
}

need_key() {
  if [ -z "${VULTR_API_KEY:-}" ] && [ -f "$ROOT/.env.local" ]; then
    VULTR_API_KEY=$(grep -E "^VULTR_API_KEY=" "$ROOT/.env.local" | head -1 | cut -d= -f2- | tr -d '\r"')
  fi
  [ -n "${VULTR_API_KEY:-}" ] || { echo "VULTR_API_KEY not set (.env.local)" >&2; return 1; }
  export VULTR_API_KEY
}

# Destroy and KEEP CHECKING until the API stops listing it. See the header.
destroy_verified() {
  local id="$1" n
  for n in 1 2 3 4 5 6; do
    api DELETE "/instances/$id" >/dev/null 2>&1
    sleep 8
    if ! api GET /instances | grep -q "$id"; then return 0; fi
    echo "    still present after attempt $n, retrying" >&2
  done
  echo "    !!! COULD NOT DESTROY $id -- do it by hand:" >&2
  echo "    curl -X DELETE https://api.vultr.com/v2/instances/$id -H \"Authorization: Bearer \$VULTR_API_KEY\"" >&2
  return 1
}

boot_one() {   # boot_one <class>   -- runs in the background, one per class
  local cls="$1" f="$FLEET/$cls" kid resp id ip
  kid=$(api GET /ssh-keys | "$PY" "$JF" ssh_keys.0.id)
  [ -n "$kid" ] || { echo "[$cls] no SSH key on the account"; return 1; }

  # Vultr rejects an underscore in a hostname, and three of the five class names have one
  # (us_stocks, us_etfs, cme_futures). The LABEL keeps the real name for the console; only
  # the hostname is sanitised.
  local host="sh-$(echo "$cls" | tr '_' '-')"
  # THE KEY GOES IN BY CLOUD-INIT, NOT BY sshkey_id. Passing a valid sshkey_id is accepted
  # and then silently ignored on this account: the instance comes up with a default_password
  # and no authorized_keys, and every ssh attempt gets "Permission denied (publickey)". The
  # API response never echoes sshkey_id either, so nothing about the create says it failed --
  # it only shows up as an unreachable box after provisioning. user_data is honoured, so the
  # public key is written by cloud-config and verified by an actual ssh before setup runs.
  local ud
  ud=$(base64 -w0 "$ROOT/cloud/cloudinit.yml")
  resp=$(api POST /instances "{\"region\":\"$REGION\",\"plan\":\"$PLAN\",\"os_id\":$OS_ID,\"sshkey_id\":[\"$kid\"],\"user_data\":\"$ud\",\"label\":\"sh-$cls\",\"hostname\":\"$host\",\"backups\":\"disabled\",\"enable_ipv6\":false}")
  id=$(echo "$resp" | "$PY" "$JF" instance.id)
  if [ -z "$id" ]; then
    echo "[$cls] create refused: $(echo "$resp" | "$PY" "$JF" error | cut -c1-90)"
    return 1
  fi
  printf 'CLASS=%s\nINSTANCE_ID=%s\nCREATED_AT=%s\nIP=\n' "$cls" "$id" "$(date +%s)" > "$f"
  echo "[$cls] created $id"

  for _ in $(seq 1 60); do
    ip=$(api GET "/instances/$id" | "$PY" "$JF" instance.main_ip)
    [ -n "$ip" ] && [ "$ip" != "0.0.0.0" ] && break
    sleep 10
  done
  if [ -z "$ip" ] || [ "$ip" = "0.0.0.0" ]; then
    echo "[$cls] no IP after 10 min -- destroying"; destroy_verified "$id"; rm -f "$f"; return 1
  fi
  sed -i "s/^IP=.*/IP=$ip/" "$f"
  echo "[$cls] ip $ip"

  local reachable=0
  for _ in $(seq 1 40); do
    if ssh -i "$KEY" $SSH_OPTS -o BatchMode=yes -o ConnectTimeout=8 "root@$ip" true 2>/dev/null; then
      reachable=1; break
    fi
    sleep 10
  done
  if [ "$reachable" != "1" ]; then
    echo "[$cls] NEVER BECAME REACHABLE -- destroying rather than leaving it to bill"
    destroy_verified "$id"; rm -f "$f"; return 1
  fi

  echo "[$cls] installing ..."
  if ! ssh -i "$KEY" $SSH_OPTS "root@$ip" "bash -s" < "$ROOT/cloud/remote/setup.sh" \
        > "$FLEET/$cls.setup.log" 2>&1; then
    echo "[$cls] SETUP FAILED (see cloud/.fleet/$cls.setup.log) -- destroying"
    destroy_verified "$id"; rm -f "$f"; return 1
  fi

  echo "[$cls] uploading its bars ..."
  # payload_manifest.py sets newline=LF explicitly, so no tr filter here. An earlier
  # version piped through `tr -d` with a literal newline inside the quotes, which
  # deleted EVERY separator: tar then saw one 56 kB filename, failed to stat it, and
  # the upload guard destroyed the box. Two escaping bugs in a row on the same line.
  "$PY" "$ROOT/cloud/payload_manifest.py" --class "$cls" > "$FLEET/$cls.files" 2>/dev/null
  # TAR OVER SSH, NOT RSYNC. Git Bash on Windows ships no rsync, and the failure was quiet in
  # the worst way: `up` reported RUNNING and the box started work against an empty data/, so a
  # missing transfer looks exactly like a box that is busy. tar and ssh are both present, and
  # the exit status is checked here rather than assumed.
  if ! tar -cf - -C "$ROOT" -T "$FLEET/$cls.files"        | ssh -i "$KEY" $SSH_OPTS "root@$ip" "mkdir -p /opt/stockhunt && tar -xf - -C /opt/stockhunt"        2>> "$FLEET/$cls.setup.log"; then
    echo "[$cls] UPLOAD FAILED -- destroying rather than scoring an empty cache"
    destroy_verified "$id"; rm -f "$f"; return 1
  fi
  # Prove the bars arrived. A box scoring nothing still costs money.
  local nfiles
  nfiles=$(ssh -i "$KEY" $SSH_OPTS "root@$ip" "find /opt/stockhunt/data -name '*.parquet' 2>/dev/null | wc -l" 2>/dev/null | tr -d '
')
  echo "[$cls] uploaded ${nfiles:-0} parquet files"
  if [ "${nfiles:-0}" -lt 10 ]; then
    echo "[$cls] TOO FEW FILES ON THE BOX -- destroying"
    destroy_verified "$id"; rm -f "$f"; return 1
  fi

  echo "[$cls] starting ..."
  scp -q -i "$KEY" $SSH_OPTS "$ROOT/cloud/remote/run_class.sh" "root@$ip:/opt/stockhunt/"
  # rules_for.py lives in the wfo folder because it imports wfo_paths, which is that
  # folder's path bootstrap and only resolves from there.
  scp -q -i "$KEY" $SSH_OPTS "$ROOT/cloud/remote/rules_for.py" "root@$ip:/opt/stockhunt/walk-forward optimization/"
  ssh -i "$KEY" $SSH_OPTS "root@$ip" \
    "cd /opt/stockhunt && chmod +x run_class.sh && setsid nohup ./run_class.sh $cls > cloud_$cls.log 2>&1 < /dev/null & sleep 2; echo ok" >/dev/null
  echo "[$cls] RUNNING"
}

cmd_up() {
  need_key || return 1
  mkdir -p "$FLEET"
  # Resume rather than refuse. A half-built fleet is precisely when this is wanted, and a
  # class that already has a state file is already running -- skipping it is the whole point.
  echo "=== bringing up: $CLASSES"
  echo "    plan $PLAN in $REGION, \$$RATE_USD/hr each"
  echo
  # In parallel: the boxes are independent, and us_stocks uploads 1.6 GB while commodities
  # uploads 0.09 GB and starts work almost immediately.
  for cls in $CLASSES; do
    if [ -f "$FLEET/$cls" ]; then echo "[$cls] already up -- skipping"; continue; fi
    boot_one "$cls" &
  done
  wait
  echo
  echo "=== fleet up. ./cloud/fleet.sh watch   to reap boxes as they finish"
}

each_box() {   # each_box <fn>  -- calls fn with CLASS INSTANCE_ID IP CREATED_AT
  local f
  for f in "$FLEET"/*; do
    case "$f" in *.log|*.files) continue ;; esac
    [ -f "$f" ] || continue
    ( . "$f"; "$1" "$CLASS" "$INSTANCE_ID" "$IP" "$CREATED_AT" )
  done
}

_cost_line() {
  local cls="$1" ip="$3" t0="$4"
  local m=$(( ( $(date +%s) - t0 ) / 60 ))
  printf "  %-13s %-15s up %2dh%02dm  \$%.2f\n" "$cls" "$ip" $((m/60)) $((m%60)) \
    "$("$PY" -c "print($m/60*$RATE_USD)")"
}
cmd_cost() {
  [ -d "$FLEET" ] || { echo "no fleet"; return 0; }
  each_box _cost_line
}

_status_line() {
  local cls="$1" ip="$3"
  echo "--- $cls ($ip)"
  ssh -i "$KEY" $SSH_OPTS -o ConnectTimeout=8 "root@$ip" \
    "test -f /opt/stockhunt/wfo-results/.finished && echo '    *** FINISHED ***'; tail -6 /opt/stockhunt/cloud_$cls.log 2>/dev/null | sed 's/^/    /'" 2>/dev/null \
    || echo "    (unreachable)"
}
cmd_status() {
  [ -d "$FLEET" ] || { echo "no fleet"; return 0; }
  cmd_cost; echo
  each_box _status_line
}

# Fetch a box, then destroy it. Fetch FIRST and only destroy if it succeeded -- a box
# destroyed before its results are home has thrown away everything it computed.
_reap_one() {
  local cls="$1" id="$2" ip="$3"
  if ! ssh -i "$KEY" $SSH_OPTS -o ConnectTimeout=8 "root@$ip" \
        "test -f /opt/stockhunt/wfo-results/.finished" 2>/dev/null; then
    return 0
  fi
  echo "  $cls finished -- fetching"
  if ssh -i "$KEY" $SSH_OPTS "root@$ip" "cd /opt/stockhunt/wfo-results && tar -cf - ."       | tar -xf - -C "$ROOT/walk-forward optimization/results/"; then
    if destroy_verified "$id"; then
      rm -f "$FLEET/$cls" "$FLEET/$cls.files"
      echo "  $cls DESTROYED, billing stopped"
    fi
  else
    echo "  $cls FETCH FAILED -- box left alive on purpose, nothing thrown away"
  fi
}
cmd_reap() { need_key || return 1; [ -d "$FLEET" ] || { echo "no fleet"; return 0; }; each_box _reap_one; }

cmd_watch() {
  need_key || return 1
  while :; do
    cmd_reap
    local left
    left=$(ls -A "$FLEET" 2>/dev/null | grep -v '\.' | wc -l)
    [ "$left" -eq 0 ] && { echo "=== fleet empty -- everything fetched and destroyed"; return 0; }
    sleep 120
  done
}

_down_one() {
  local cls="$1" id="$2"
  echo "  destroying $cls ($id)"
  destroy_verified "$id" && rm -f "$FLEET/$cls" "$FLEET/$cls.files"
}
cmd_down() {
  need_key || return 1
  [ -d "$FLEET" ] || { echo "no fleet"; return 0; }
  cmd_cost; echo
  each_box _down_one
  echo
  echo "=== instances still on the account ==="
  api GET /instances | "$PY" -c "
import sys,json
d=json.load(sys.stdin).get('instances',[])
print('  none -- clean, billing stopped' if not d else '\n'.join('  LIVE '+i['id']+'  '+i['label'] for i in d))"
}

# The account enforces TWO caps, and they were found by probing rather than documented:
#   * per instance  ~$96-119/mo  -- $96 accepted, $120 refused
#   * account total ~$212-272/mo -- two $96 boxes accepted, a third refused;
#                                   six $20 boxes ($120) accepted
# So the fleet runs in WAVES of MAX_BOXES, not all five at once. Each wave is reaped (fetched
# and destroyed) before the next boots, which also means a class stops costing money the
# moment it is done rather than when the slowest one is.
MAX_BOXES="${MAX_BOXES:-2}"

cmd_run() {
  need_key || return 1
  mkdir -p "$FLEET"
  local pending="$CLASSES" running booted
  while [ -n "$pending" ]; do
    running=$(ls -A "$FLEET" 2>/dev/null | grep -v '\.' | wc -l)
    booted=""
    for cls in $pending; do
      [ "$running" -ge "$MAX_BOXES" ] && break
      [ -f "$FLEET/$cls" ] && continue
      boot_one "$cls"
      if [ -f "$FLEET/$cls" ]; then
        running=$((running + 1))
        booted="$booted $cls"
      else
        echo "[$cls] could not boot -- will retry next wave"
      fi
    done
    [ -n "$booted" ] && echo "=== wave up:$booted"
    # Reap on a loop until a slot frees, then boot into it.
    while :; do
      sleep 120
      cmd_reap
      local now
      now=$(ls -A "$FLEET" 2>/dev/null | grep -v '\.' | wc -l)
      [ "$now" -lt "$MAX_BOXES" ] && break
    done
    # Anything with no state file is either finished-and-reaped or never booted.
    local still=""
    for cls in $pending; do
      [ -f "$FLEET/$cls" ] && { still="$still $cls"; continue; }
      [ -f "$ROOT/walk-forward optimization/results/edge_cells/edge_${cls}_5m.csv" ] || still="$still $cls"
    done
    pending="$(echo $still)"
    echo "=== still to do:${pending:- none}"
  done
  echo "=== ALL CLASSES DONE"
}

case "${1:-}" in
  up) cmd_up ;;
  run) cmd_run ;;
  status) cmd_status ;;
  reap) cmd_reap ;;
  watch) cmd_watch ;;
  down) cmd_down ;;
  cost) cmd_cost ;;
  *) sed -n "2,10p" "$0"; echo "  ./cloud/fleet.sh run      waves of MAX_BOXES until every class is done"; exit 1 ;;
esac
