#!/usr/bin/env bash
#
# system-test.sh — end-to-end smoke test of the dev container cluster.
#
# Exercises the integration seams between the components the dev cluster
# carries: etcd (mTLS quorum), the ycluster CLI, leader elections (DHCP +
# storage), keepalived/VIP, the docker registry, postgres, authentik,
# rathole (client on the storage leader + ssh clients on all core nodes,
# server on f1), the admin API (core + non-core), the admin web UI's
# forward-auth gate, incus VM management (nested incus on c1: launch →
# sample → desired-state → reconcile), usage accounting (etcd → postgres),
# and local-ai-proxy (etcd-routed models, nginx auth_request, hot reload).
#
# Deliberately NOT covered (can't run in system containers / on a laptop):
# Ceph, real ACME/certbot, the PXE/autoinstall boot path, GPU passthrough,
# Open-WebUI, the monitoring stack.
#
# Usage:
#   ./system-test.sh                 # up + provision + assert (idempotent)
#   ./system-test.sh --assert-only   # skip dev-cluster.sh up + playbooks
#   ./system-test.sh --no-disruptive # skip failover tests (leader/VIP moves)
#
# Sections run independently: a failed section is recorded and the test
# moves on, so one broken seam doesn't hide the state of the others.
# Exit code is non-zero if any section failed.
#
# Dev caveat: the /rbd stand-ins are node-local, so leader-local
# provisioning (authentik DB, usage_stats) only exists on the node that
# led during app-dev.yml. The failover section restores leadership to the
# section-start leader, but if leadership drifted between runs,
# --assert-only can fail on those legs — run the full mode (it provisions
# the current leader) to converge.
set -uo pipefail

cd "$(dirname "$0")"

# -n: remote commands must not inherit the terminal's stdin — some of them
# (incus via ycluster) read a non-TTY stdin to EOF and hang an interactive run.
SSH="ssh -n -F .ssh/config"
PLAYBOOK="../../venv/bin/ansible-playbook"
CORE=(s1 s2 s3)
declare -A IP=( [s1]=10.0.0.11 [s2]=10.0.0.12 [s3]=10.0.0.13 [c1]=10.0.0.51 [f1]=10.0.0.41 )
VIP=10.0.0.100
TLSDIR=/etc/etcd/tls
CERTS="--cacert $TLSDIR/ca.crt --cert $TLSDIR/client.crt --key $TLSDIR/client.key"
# Sourcing the canonical client env gives etcdctl AND the ycluster CLI
# their endpoints + mTLS certs, exactly as cluster services get them.
E='set -a; . /etc/ycluster/etcd-client.env; set +a;'

ASSERT_ONLY=false
DISRUPTIVE=true
for arg in "$@"; do
  case "$arg" in
    --assert-only)   ASSERT_ONLY=true ;;
    --no-disruptive) DISRUPTIVE=false ;;
    *) echo "usage: $0 [--assert-only] [--no-disruptive]" >&2; exit 1 ;;
  esac
done

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32mPASS\033[0m %s\n' "$*"; }
die()  { printf '   \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# retry <tries> <sleep_s> <label> <cmd...> — poll until cmd succeeds.
retry() {
  local tries=$1 pause=$2 label=$3 i; shift 3
  for ((i = 1; i <= tries; i++)); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep "$pause"
  done
  die "$label (gave up after $tries x ${pause}s)"
}

app_leader() { $SSH s1 "$E etcdctl get /cluster/leader/app --print-value-only" 2>/dev/null | tr -d '[:space:]'; }

declare -a FAILED=()
SECTIONS=0
section() {  # $1=name $2=fn — run fn in a subshell; record, don't abort
  local name=$1 fn=$2
  SECTIONS=$((SECTIONS + 1))
  log "$name"
  if ! ( set -e; "$fn" ); then
    FAILED+=("$name")
    printf '   \033[1;31m== SECTION FAILED:\033[0m %s\n' "$name" >&2
  fi
}

# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------
provision() {
  log "PROVISION: dev-cluster.sh up + site-dev + collect-hw + app-dev"
  ./dev-cluster.sh up
  $PLAYBOOK site-dev.yml      > /tmp/system-test-site-dev.log 2>&1 \
    || die "site-dev.yml failed (see /tmp/system-test-site-dev.log)"
  ok "site-dev.yml"
  $PLAYBOOK collect-hw-dev.yml > /tmp/system-test-collect-hw.log 2>&1 \
    || die "collect-hw-dev.yml failed (see /tmp/system-test-collect-hw.log)"
  ok "collect-hw-dev.yml"
  $PLAYBOOK app-dev.yml       > /tmp/system-test-app-dev.log 2>&1 \
    || die "app-dev.yml failed (see /tmp/system-test-app-dev.log)"
  ok "app-dev.yml"
}

# --------------------------------------------------------------------------
# 1. substrate + etcd quorum (mTLS)
# --------------------------------------------------------------------------
sec_etcd() {
  for n in s1 s2 s3 c1 f1; do
    $SSH "$n" true 2>/dev/null || die "ssh to $n"
  done
  ok "all 5 nodes reachable over ssh"

  # ENVU: the nodes' login env exports ETCDCTL_* vars, which etcdctl treats
  # as fatal conflicts with the corresponding explicit flags.
  local ENVU="env -u ETCDCTL_ENDPOINTS -u ETCDCTL_CACERT -u ETCDCTL_CERT -u ETCDCTL_KEY"
  for n in "${CORE[@]}"; do
    $SSH "$n" "systemctl is-active --quiet etcd" || die "etcd not active on $n"
    $SSH "$n" "$ENVU etcdctl --endpoints https://${IP[$n]}:2381 $CERTS endpoint health" \
      >/dev/null 2>&1 || die "etcd endpoint health on $n"
  done
  ok "3/3 etcd members healthy over mTLS"

  local nonce="smoke-$$-$RANDOM"
  $SSH s1 "$E etcdctl put /smoke/etcd $nonce" >/dev/null || die "quorum write on s1"
  [ "$($SSH s3 "$E etcdctl get /smoke/etcd --print-value-only")" = "$nonce" ] \
    || die "replicated read on s3"
  $SSH s1 "$E etcdctl del /smoke/etcd" >/dev/null
  ok "write on s1 replicates to s3"

  $SSH s1 "$ENVU etcdctl --endpoints http://${IP[s1]}:2379 --command-timeout=3s endpoint health" \
    >/dev/null 2>&1 && die "plaintext :2379 still answering"
  ok "plaintext :2379 refused"
  $SSH s1 "$ENVU etcdctl --endpoints https://${IP[s1]}:2381 --cacert $TLSDIR/ca.crt --command-timeout=3s endpoint health" \
    >/dev/null 2>&1 && die "TLS :2381 accepted connection without client cert"
  ok "TLS :2381 without client cert refused"
}

# --------------------------------------------------------------------------
# 2. ycluster CLI round-trips (config CRUD against etcd)
# --------------------------------------------------------------------------
sec_cli() {
  $SSH s1 "$E ycluster cluster status" >/dev/null || die "ycluster cluster status"
  ok "cluster status"

  $SSH s1 "$E ycluster tls set-common-name smoke.dev.test" >/dev/null \
    || die "tls set-common-name"
  $SSH s1 "$E ycluster tls get-common-name" | grep -q smoke.dev.test \
    || die "tls get-common-name readback"
  $SSH s1 "$E ycluster tls generate --common-name smoke.dev.test" >/dev/null \
    || die "tls generate"
  $SSH s1 "$E ycluster tls get" | grep -q 'BEGIN CERTIFICATE' \
    || die "tls get returns a certificate"
  ok "tls set-common-name / generate / get"

  $SSH s1 "$E ycluster healthchecks set-url https://hc-ping.com/smoke-dummy" >/dev/null \
    || die "healthchecks set-url"
  $SSH s1 "$E ycluster healthchecks get-url" | grep -q smoke-dummy || die "healthchecks get-url"
  $SSH s1 "$E ycluster healthchecks delete-url" >/dev/null || die "healthchecks delete-url"
  ok "healthchecks set-url / get-url / delete-url"

  # Example key from the age README — format-valid, owned by nobody.
  local agekey=age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p
  $SSH s1 "$E ycluster backup recipients remove smoke-recipient" >/dev/null 2>&1 || true
  $SSH s1 "$E ycluster backup recipients add smoke-recipient $agekey" >/dev/null \
    || die "backup recipients add"
  $SSH s1 "$E ycluster backup recipients list" | grep -q smoke-recipient \
    || die "backup recipients list"
  $SSH s1 "$E ycluster backup recipients remove smoke-recipient" >/dev/null \
    || die "backup recipients remove"
  ok "backup recipients add / list / remove"

  $SSH s1 "$E ycluster dhcp list all" >/dev/null || die "dhcp list all"
  ok "dhcp list all (etcd read path)"
}

# --------------------------------------------------------------------------
# 3. frontend management
# --------------------------------------------------------------------------
sec_frontend() {
  $SSH s1 "$E ycluster frontend delete smoke-f9" >/dev/null 2>&1 || true  # stale runs
  $SSH s1 "$E ycluster frontend add smoke-f9 10.0.0.49" >/dev/null \
    || die "frontend add"
  $SSH s1 "$E ycluster frontend show smoke-f9" | grep -q 10.0.0.49 \
    || die "frontend show"
  $SSH s1 "$E ycluster frontend list" | grep -q smoke-f9 || die "frontend list"
  $SSH s1 "$E ycluster frontend delete smoke-f9" >/dev/null || die "frontend delete"
  $SSH s1 "$E ycluster frontend list" | grep -q smoke-f9 \
    && die "frontend still listed after delete"
  ok "frontend add / show / list / delete"
  # f1 itself is registered by app-dev's fixture
  $SSH s1 "$E ycluster frontend list" | grep -q f1 || die "f1 not registered"
  ok "f1 registered as frontend"
}

# --------------------------------------------------------------------------
# 4. admin-api (core + non-core) + inventory
# --------------------------------------------------------------------------
sec_admin_api() {
  # From the NON-core node c1: its own nginx (local mode) and the VIP.
  # /api/status returns allocation counts per node type.
  for url in "http://localhost/api/status" "http://admin.xc/api/status"; do
    $SSH c1 "curl -fsS --max-time 10 $url" | grep -q '"compute"' \
      || die "GET $url from c1"
  done
  ok "/api/status via c1-local nginx and via admin.xc (VIP)"

  # /api/health legitimately reports unhealthy (503) in dev: ntp/dns/squid
  # come from base-infrastructure, which is deliberately not container-safe.
  # Assert the health document itself, not the verdict.
  retry 5 2 "/api/health on c1" \
    $SSH c1 "curl -sS --max-time 10 http://localhost/api/health | grep -q '\"overall\"'"
  # /metrics is scraped straight off the Flask port, not through nginx.
  retry 5 2 "/metrics on c1 (flask :12723)" \
    $SSH c1 "curl -fsS --max-time 10 http://127.0.0.1:12723/metrics | grep -q ycluster"
  ok "/api/health + /metrics on c1"

  # Allocation from a non-core node — the exact etcd write path the
  # hardening work gates. Allocate, verify registration, then free it.
  local mac="02:de:ad:00:00:09"
  local out hostname
  out=$($SSH c1 "curl -fsS --max-time 10 'http://localhost/api/allocate?mac=$mac&type=compute'") \
    || die "/api/allocate from c1"
  hostname=$(echo "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hostname"])') \
    || die "allocate response unparsable: $out"
  # The stored record holds the normalized (colon-less) MAC.
  $SSH s1 "$E etcdctl get /cluster/nodes/by-hostname/$hostname --print-value-only" \
    | grep -q "${mac//:/}" || die "allocation not in etcd"
  # One key per del — a second arg to etcdctl del is a RANGE END.
  # by-mac keys use the normalized (colon-less) MAC.
  $SSH s1 "$E etcdctl del /cluster/nodes/by-hostname/$hostname" >/dev/null
  $SSH s1 "$E etcdctl del /cluster/nodes/by-mac/${mac//:/}" >/dev/null
  ok "/api/allocate from c1 ($hostname) registered in etcd, cleaned up"

  # Inventory: collect-hw-dev.yml pushed hardware facts during provisioning.
  $SSH s1 "$E ycluster inventory show" | grep -q c1 || die "ycluster inventory show lists c1"
  $SSH c1 "curl -fsS --max-time 10 http://localhost/api/inventory" | grep -q '"c1"' \
    || die "/api/inventory lists c1"
  ok "hardware inventory in etcd, CLI and API agree"
}

# --------------------------------------------------------------------------
# 5. storage leader election + VIP + leader-only services
# --------------------------------------------------------------------------
sec_leader() {
  local leader
  leader=$(app_leader)
  [ -n "$leader" ] || die "no /cluster/leader/app"
  ok "storage leader elected: $leader"

  $SSH "$leader" "mountpoint -q /rbd/user && mountpoint -q /rbd/misc" \
    || die "leader missing /rbd mounts"
  for svc in postgresql@16-main docker-registry rathole authentik qdrant user-rbd misc-rbd; do
    $SSH "$leader" "systemctl is-active --quiet $svc" || die "$svc not active on leader"
  done
  ok "leader runs postgres, registry, rathole, authentik, qdrant, rbd mounts"

  for n in "${CORE[@]}"; do
    [ "$n" = "$leader" ] && continue
    $SSH "$n" "systemctl is-active --quiet postgresql@16-main" \
      && die "postgres active on non-leader $n"
    $SSH "$n" "systemctl is-active --quiet authentik" \
      && die "authentik active on non-leader $n"
  done
  ok "non-leaders run no leader-only services"

  local holders=0
  for n in "${CORE[@]}"; do
    if $SSH "$n" "ip -br addr show eth0" | grep -q "$VIP"; then
      holders=$((holders + 1))
      [ "$n" = "$leader" ] || die "VIP on non-leader $n"
    fi
  done
  [ "$holders" = 1 ] || die "VIP holder count = $holders (want exactly 1)"
  ok "storage VIP on the leader only"

  $SSH c1 "curl -fsS --max-time 5 http://$VIP:5000/v2/" >/dev/null \
    || die "registry not answering via VIP"
  ok "docker registry answers on the VIP (keepalived gate satisfied)"
}

# --------------------------------------------------------------------------
# 6. DHCP leader election (+ disruptive failover)
# --------------------------------------------------------------------------
sec_dhcp_election() {
  local holder
  holder=$($SSH s1 "$E etcdctl get /cluster/leader/dhcp --print-value-only" | tr -d '[:space:]')
  [ -n "$holder" ] || die "no /cluster/leader/dhcp"
  ok "dhcp leader: $holder"

  $DISRUPTIVE || { ok "(failover skipped: --no-disruptive)"; return 0; }

  $SSH "$holder" "systemctl stop dhcp-leader-election"
  local new=""
  for i in $(seq 1 30); do
    new=$($SSH s1 "$E etcdctl get /cluster/leader/dhcp --print-value-only" | tr -d '[:space:]')
    [ -n "$new" ] && [ "$new" != "$holder" ] && break
    sleep 2
  done
  $SSH "$holder" "systemctl start dhcp-leader-election"   # restore before judging
  [ -n "$new" ] && [ "$new" != "$holder" ] || die "dhcp leadership did not fail over"
  ok "dhcp leadership failed over $holder -> $new; $holder rejoined"
}

# --------------------------------------------------------------------------
# 7. postgres + authentik + user management (CLI -> IdP API -> postgres)
# --------------------------------------------------------------------------
sec_authentik() {
  local leader
  leader=$(app_leader)
  $SSH "$leader" 'su - postgres -c "psql -Atl"' | cut -d'|' -f1 | grep -qx authentik \
    || die "authentik database missing"
  ok "authentik database present on leader postgres"

  # Retried: after a leadership change the apps target takes ~a minute.
  retry 20 3 "authentik health via auth.xc (VIP)" \
    $SSH c1 "curl -fsS --max-time 10 http://auth.xc/-/health/live/ -o /dev/null"
  ok "authentik live via auth.xc"

  # CLI -> authentik API (token from etcd) -> postgres, asserted at each hop.
  # Fixed address keeps re-runs idempotent ('already exists' is fine).
  local email="smoke-user@dev.test"
  local add_out
  add_out=$($SSH "$leader" "$E ycluster user add $email --name 'Smoke User'" 2>&1) \
    || echo "$add_out" | grep -qiE 'exist|unique' || die "ycluster user add: $add_out"
  $SSH "$leader" "$E ycluster user list" | grep -q "$email" || die "user not in list"
  $SSH "$leader" "su - postgres -c \"psql -At authentik -c \\\"select count(*) from authentik_core_user where email='$email'\\\"\"" \
    | grep -qx 1 || die "user row not in authentik postgres"
  ok "ycluster user add -> authentik API -> postgres row"

  $SSH "$leader" "$E ycluster user admin $email" >/dev/null || die "ycluster user admin"
  ok "ycluster user admin (group membership via API)"

  # Invitation: creates an enrollment-flow link on the external domain.
  local invite
  $SSH "$leader" "$E ycluster user uninvite smoke-invitee@dev.test" >/dev/null 2>&1 || true
  invite=$($SSH "$leader" "$E ycluster user invite smoke-invitee@dev.test" 2>&1) \
    || die "ycluster user invite: $invite"
  echo "$invite" | grep -q 'itoken=' || die "invite printed no enrollment link: $invite"
  $SSH "$leader" "$E ycluster user uninvite smoke-invitee@dev.test" >/dev/null \
    || die "ycluster user uninvite"
  ok "ycluster user invite / uninvite (enrollment link via IdP API)"
}

# --------------------------------------------------------------------------
# 8. incus vm lifecycle on c1 (launch -> sample -> desired -> reconcile)
# --------------------------------------------------------------------------
sec_vm() {
  local vm=smokevm owner=smoke-owner
  $SSH c1 "systemctl is-active --quiet vm-state-sampler.timer && systemctl is-active --quiet vm-reconciler.timer" \
    || die "sampler/reconciler timers not active on c1"
  ok "vm-state-sampler + vm-reconciler timers active"

  # Throwaway owner key (registered in /cluster/users/, injected into the vm)
  [ -f .ssh/id_smoke ] || ssh-keygen -t ed25519 -N '' -f .ssh/id_smoke -C smoke >/dev/null
  $SSH c1 "$E ycluster vm ssh add $owner '$(cat .ssh/id_smoke.pub)'" >/dev/null \
    || die "vm ssh add"
  $SSH c1 "$E ycluster vm ssh list" | grep -q "$owner" || die "vm ssh list"
  ok "owner key registered in /cluster/users/"

  # Idempotent re-runs: clear any leftover instance from a failed pass.
  $SSH c1 "$E ycluster vm destroy $vm" >/dev/null 2>&1 || true
  $SSH s1 "$E etcdctl del /cluster/vm-desired/$vm" >/dev/null
  $SSH s1 "$E etcdctl del /cluster/vm-grace/$vm" >/dev/null

  # The real launch path: nested incus container, IP pin, sshd, owner keys.
  $SSH c1 "$E ycluster vm launch $vm --owner $owner --gpus 0 --cpu 1 --mem 1GiB --image images:ubuntu/24.04/cloud" \
    >/dev/null || die "ycluster vm launch"
  $SSH c1 "incus list $vm --format json" | grep -q '"status": *"Running"' \
    || die "instance not running after launch"
  $SSH s1 "$E etcdctl get /cluster/vms/$vm --print-value-only" | grep -q '"state": "ready"' \
    || die "vm record not ready in etcd"
  ok "vm launch (nested incus container, etcd registration)"

  $SSH c1 "$E ycluster vm sample" >/dev/null || die "vm sample"
  $SSH s1 "$E etcdctl get /cluster/vm-state/c1 --print-value-only" \
    | grep -q "\"$vm\"" || die "sample snapshot missing $vm"
  ok "vm sample -> /cluster/vm-state/c1"

  # desired=off: first reconcile warns + stamps grace, instance stays up.
  $SSH s1 "$E etcdctl put /cluster/vm-desired/$vm '{\"mode\": \"off\"}'" >/dev/null
  $SSH c1 "$E ycluster vm reconcile" >/dev/null || die "reconcile (warn tick)"
  $SSH s1 "$E etcdctl get /cluster/vm-grace/$vm --print-value-only" | grep -q warned_at \
    || die "grace marker not stamped"
  $SSH c1 "incus list $vm --format json" | grep -q '"status": *"Running"' \
    || die "instance stopped before grace elapsed"
  ok "reconcile warn tick: grace stamped, instance still up"

  # Backdate the warning so the next tick's grace check has elapsed.
  local past
  past=$($SSH c1 "date -u -d '-10 minutes' +%Y-%m-%dT%H:%M:%S+00:00")
  $SSH s1 "$E etcdctl put /cluster/vm-grace/$vm '{\"warned_at\": \"$past\"}'" >/dev/null
  $SSH c1 "$E ycluster vm reconcile" >/dev/null || die "reconcile (stop tick)"
  $SSH c1 "incus list $vm --format json" | grep -q '"status": *"Stopped"' \
    || die "instance not stopped after grace"
  ok "reconcile stop tick: clean stop after grace"

  # desired=on: reconcile starts it again (billable scheduler start).
  $SSH s1 "$E etcdctl put /cluster/vm-desired/$vm '{\"mode\": \"on\"}'" >/dev/null
  $SSH c1 "$E ycluster vm reconcile" >/dev/null || die "reconcile (start)"
  $SSH c1 "incus list $vm --format json" | grep -q '"status": *"Running"' \
    || die "instance not started by reconcile"
  ok "reconcile start: desired=on brings it back"

  $SSH c1 "$E ycluster vm destroy $vm" >/dev/null || die "vm destroy"
  $SSH s1 "$E etcdctl del /cluster/vm-desired/$vm" >/dev/null
  $SSH s1 "$E etcdctl del /cluster/vm-grace/$vm" >/dev/null
  $SSH s1 "$E etcdctl get /cluster/vms/$vm --print-value-only" | grep -q . \
    && die "vm record survived destroy"
  ok "vm destroy: instance + etcd record gone"
}

# --------------------------------------------------------------------------
# 9. usage accounting (vm events/samples -> postgres on the leader)
# --------------------------------------------------------------------------
sec_usage() {
  local leader
  leader=$(app_leader)
  # Force a collector run now instead of waiting for the timer.
  $SSH "$leader" "systemctl start collect-vm-stats.service" || die "collect-vm-stats run"
  local events
  events=$($SSH "$leader" "su - postgres -c \"psql -At usage_stats -c \\\"select count(*) from vm_events where vm='smokevm'\\\"\"")
  [ "${events:-0}" -ge 3 ] || die "expected >=3 smokevm lifecycle events, got '$events'"
  ok "vm_events drained to postgres ($events smokevm events)"

  $SSH s1 "$E etcdctl get --prefix --keys-only /cluster/vms-events/" | grep -q . \
    && die "vms-events not drained from etcd"
  ok "etcd event queue drained (exactly-once handoff)"

  local samples
  samples=$($SSH "$leader" "su - postgres -c \"psql -At usage_stats -c \\\"select count(*) from vm_samples where vm='smokevm'\\\"\"")
  [ "${samples:-0}" -ge 1 ] || die "no smokevm vm_samples rows"
  ok "vm_samples rows from /cluster/vm-state/ snapshots ($samples)"
}

# --------------------------------------------------------------------------
# 10. rathole tunnels (client on leader + ssh clients -> server on f1)
# --------------------------------------------------------------------------
sec_rathole() {
  for n in "${CORE[@]}"; do
    $SSH "$n" "systemctl is-active --quiet rathole-ssh" || die "rathole-ssh not active on $n"
  done
  ok "rathole-ssh clients active on all core nodes"

  # Each core node's sshd is reachable through f1's per-node tunnel port
  # (2201..2203, bound to f1 localhost) — read the banner through the tunnel.
  for n in "${CORE[@]}"; do
    local port=$((2200 + ${n#s}))
    $SSH f1 "timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/$port; head -c 7 <&3'" \
      | grep -q '^SSH-2.0' || die "no ssh banner via f1:$port (tunnel for $n)"
  done
  ok "ssh banners via f1 tunnel ports 2201-2203"

  # The leader's rathole client carries f1:80 to the leader's public vhost
  # (127.0.0.2:80). /status is a public page served by admin-api.
  retry 15 2 "http through f1:80 tunnel" \
    $SSH c1 "curl -fsS --max-time 5 http://${IP[f1]}/status -o /dev/null"
  ok "public http through f1 -> leader public vhost"
}

# --------------------------------------------------------------------------
# 11. admin web UI forward-auth gate (nginx auth_request -> authentik outpost)
# --------------------------------------------------------------------------
sec_admin_web() {
  # The proxy provider matches requests by external host (forward_single),
  # so public requests must present the admin.<domain> Host — exactly what
  # a browser sends on the real cluster.
  local domain admin_host
  domain=$($SSH s1 "$E etcdctl get /cluster/https/domain --print-value-only" | tr -d '[:space:]')
  [ -n "$domain" ] || die "no external domain in etcd (app-dev fixture missing)"
  admin_host="admin.$domain"

  local loc
  loc=$($SSH c1 "curl -s --max-time 10 -o /dev/null -w '%{http_code} %{redirect_url}' -H 'Host: $admin_host' http://${IP[f1]}/admin/")
  echo "$loc" | grep -q '^302 .*outpost.goauthentik.io/start' \
    || die "/admin/ not gated (got: $loc)"
  ok "/admin/ via f1 redirects anonymous to authentik outpost"

  # The outpost start endpoint must itself be served (proxied to authentik
  # on the leader) and hand the browser on to the IdP's login flow.
  local code
  code=$($SSH c1 "curl -s --max-time 10 -o /dev/null -w '%{http_code}' -H 'Host: $admin_host' 'http://${IP[f1]}/outpost.goauthentik.io/start?rd=http://$admin_host/admin/'")
  [ "$code" = 302 ] || die "outpost start returned $code"
  ok "outpost endpoint proxied to authentik (login redirect works)"

  # Same endpoint, internal vs public: /api/inventory is open inside the
  # cluster (asserted in section 4) but auth-gated on the public vhost.
  code=$($SSH c1 "curl -s --max-time 10 -o /dev/null -w '%{http_code}' -H 'Host: $admin_host' http://${IP[f1]}/api/inventory")
  [ "$code" = 302 ] || die "/api/inventory not gated on public vhost (got $code)"
  ok "/api/inventory: open internally, gated publicly"
}

# --------------------------------------------------------------------------
# 12. local-ai-proxy (etcd-routed model, nginx auth_request, hot reload)
# --------------------------------------------------------------------------
sec_inference() {
  for n in "${CORE[@]}"; do
    $SSH "$n" "systemctl is-active --quiet local-ai-proxy" \
      || die "local-ai-proxy not active on $n"
  done
  ok "local-ai-proxy active on all storage nodes"

  # Master key: seed once (idempotent across runs).
  local master
  master=$($SSH s1 "$E etcdctl get /cluster/config/inference/master-key --print-value-only" | tr -d '[:space:]')
  if [ -z "$master" ]; then
    master="smoke-$(openssl rand -hex 16)"
    $SSH s1 "$E etcdctl put /cluster/config/inference/master-key $master" >/dev/null
  fi

  # Route the dev-echo model at c1's stub backend, via the CLI.
  $SSH s1 "$E ycluster inference add http://${IP[c1]}:8000 dev-echo" >/dev/null \
    || die "ycluster inference add"
  retry 10 1 "dev-echo appears in /v1/models (hot reload)" \
    $SSH c1 "curl -fsS --max-time 5 -H 'Authorization: Bearer $master' http://inference.xc/v1/models | grep -q dev-echo"
  ok "model added via CLI, hot-reloaded, served through inference.xc (VIP + auth)"

  # Retried generously: after a fresh model add the proxy lists the model
  # immediately but routes completions only once its first backend health
  # probe has passed.
  retry 15 2 "chat completion through proxy + stub backend" \
    $SSH c1 "curl -fsS --max-time 10 -H 'Authorization: Bearer $master' -H 'Content-Type: application/json' \
      -d '{\"model\":\"dev-echo\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}' \
      http://inference.xc/v1/chat/completions | grep -q dev-stub-reply"
  ok "chat completion: nginx auth -> proxy -> stub backend -> response"

  local code
  code=$($SSH c1 "curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://inference.xc/v1/models")
  [ "$code" = 401 ] || die "unauthenticated request got $code (want 401)"
  code=$($SSH c1 "curl -s --max-time 5 -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer wrong-key' http://inference.xc/v1/models")
  [ "$code" = 401 ] || die "bad bearer got $code (want 401)"
  ok "missing/wrong bearer rejected by auth_request"

  $SSH s1 "$E ycluster inference remove dev-echo" >/dev/null || die "inference remove"
  retry 10 1 "dev-echo gone from /v1/models (hot reload)" \
    bash -c "! $SSH c1 \"curl -fsS --max-time 5 -H 'Authorization: Bearer $master' http://inference.xc/v1/models\" | grep -q dev-echo"
  ok "model removal hot-reloaded"
}

# --------------------------------------------------------------------------
# 13. storage leadership + VIP failover (drain) and failback  [disruptive]
# --------------------------------------------------------------------------
sec_failover() {
  $DISRUPTIVE || { ok "(skipped: --no-disruptive)"; return 0; }

  local old new
  old=$(app_leader)
  [ -n "$old" ] || die "no leader before failover"

  # Belt and braces: no stale drains from a previous broken run.
  for n in "${CORE[@]}"; do
    $SSH s1 "$E etcdctl del /cluster/nodes/$n/drain" >/dev/null
  done

  $SSH s1 "$E etcdctl put /cluster/nodes/$old/drain true" >/dev/null
  local i
  for i in $(seq 1 45); do
    new=$(app_leader)
    [ -n "$new" ] && [ "$new" != "$old" ] && break
    sleep 2
  done
  [ -n "$new" ] && [ "$new" != "$old" ] || die "leadership did not move off drained $old"
  ok "drain $old -> leadership moved to $new"

  retry 30 3 "VIP follows leadership to $new" \
    $SSH "$new" "ip -br addr show eth0 | grep -q $VIP"
  $SSH "$old" "ip -br addr show eth0" | grep -q "$VIP" && die "VIP still on drained $old"
  ok "VIP moved to $new (and left $old)"

  retry 30 3 "registry via VIP on new leader" \
    $SSH c1 "curl -fsS --max-time 5 http://$VIP:5000/v2/ -o /dev/null"
  ok "registry serving via VIP on $new"

  retry 30 3 "public http tunnel re-established via $new" \
    $SSH c1 "curl -fsS --max-time 5 http://${IP[f1]}/status -o /dev/null"
  ok "rathole client re-homed: f1:80 serves again"

  # Failback: the election is first-come, so make the outcome deterministic
  # by draining EVERY candidate except the original. The original holds the
  # only populated dev postgres/authentik state, so steady state must
  # return there before the apps can be asserted healthy again.
  for n in "${CORE[@]}"; do
    if [ "$n" = "$old" ]; then
      $SSH s1 "$E etcdctl del /cluster/nodes/$n/drain" >/dev/null
    else
      $SSH s1 "$E etcdctl put /cluster/nodes/$n/drain true" >/dev/null
    fi
  done
  local back=""
  for i in $(seq 1 45); do
    back=$(app_leader)
    [ "$back" = "$old" ] && break
    sleep 2
  done
  for n in "${CORE[@]}"; do
    $SSH s1 "$E etcdctl del /cluster/nodes/$n/drain" >/dev/null
  done
  [ "$back" = "$old" ] || die "leadership did not return to $old (got '$back')"
  ok "failback: leadership returned to $old"

  retry 30 3 "VIP back on $old" $SSH "$old" "ip -br addr show eth0 | grep -q $VIP"
  retry 60 3 "authentik healthy again after failback" \
    $SSH c1 "curl -fsS --max-time 5 http://auth.xc/-/health/live/ -o /dev/null"
  ok "VIP + authentik healthy again on $old"
}

# --------------------------------------------------------------------------
main() {
  $ASSERT_ONLY || provision

  section "1. substrate + etcd quorum (mTLS)"            sec_etcd
  section "2. ycluster CLI round-trips"                  sec_cli
  section "3. frontend management"                       sec_frontend
  section "4. admin-api (core + non-core) + inventory"   sec_admin_api
  section "5. storage leader + VIP + leader services"    sec_leader
  section "6. dhcp leader election"                      sec_dhcp_election
  section "7. postgres + authentik + user management"    sec_authentik
  section "8. incus vm lifecycle (c1)"                   sec_vm
  section "9. usage accounting -> postgres"              sec_usage
  section "10. rathole tunnels via f1"                   sec_rathole
  section "11. admin web forward-auth gate"              sec_admin_web
  section "12. local-ai-proxy inference path"            sec_inference
  section "13. leadership + VIP failover/failback"       sec_failover

  log "SUMMARY"
  if [ ${#FAILED[@]} -eq 0 ]; then
    ok "all $SECTIONS sections passed"
  else
    printf '   \033[1;31mFAIL\033[0m %d/%d sections:\n' ${#FAILED[@]} $SECTIONS >&2
    printf '     - %s\n' "${FAILED[@]}" >&2
    exit 1
  fi
}

main
