#!/usr/bin/env bash
#
# Rehearse the etcd plaintext -> mTLS migration on the dev container cluster,
# the way it will run on the real cluster: starting from a LIVE plaintext
# 3-node cluster and advancing one phase at a time, rolling each phase out one
# node at a time, asserting quorum survives every single step.
#
# This is the test that the fresh-bootstrap dev run (site-dev.yml) cannot give
# us: it exercises the TRANSITION, where a TLS node must coexist with plaintext
# nodes. See docs/design/etcd-access-hardening.md.
#
# Phases (see group_vars/all/main.yml etcd_tls_phase):
#   off -> listen -> connect -> enforce
#
# Usage:
#   ./etcd-tls-migrate.sh              # full run: wipe to plaintext, then migrate
#   ./etcd-tls-migrate.sh --from-here  # skip the wipe/bootstrap; migrate from
#                                      # whatever phase the cluster is at now
#
# Destructive: the default run STOPS etcd and WIPES /var/lib/etcd on s1-s3 to
# start from a clean plaintext cluster. Dev containers only.
set -euo pipefail

cd "$(dirname "$0")"

SSH="ssh -F .ssh/config"
PLAYBOOK="../../venv/bin/ansible-playbook"
NODES=(s1 s2 s3)
declare -A IP=( [s1]=10.0.0.11 [s2]=10.0.0.12 [s3]=10.0.0.13 )
TLSDIR=/etc/etcd/tls
CERTS="--cacert $TLSDIR/ca.crt --cert $TLSDIR/client.crt --key $TLSDIR/client.key"

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32mPASS\033[0m %s\n' "$*"; }
die()  { printf '   \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# etcdctl invocation for a phase: plaintext until the cluster connects over TLS
# (listen still serves plaintext), TLS+certs once at connect/enforce.
etcdctl_for() {  # $1=phase  $2=node -> echoes the remote etcdctl prefix
  local phase=$1 node=$2 ip=${IP[$2]}
  case "$phase" in
    off|listen) echo "etcdctl --endpoints http://$ip:2379" ;;
    connect|enforce) echo "etcdctl --endpoints https://$ip:2381 $CERTS" ;;
  esac
}

# Assert every node is up AND the cluster has a leader / can serve a quorum
# write, via each node's own currently-correct endpoint.
health_check() {  # $1=phase  $2=label
  local phase=$1 label=$2
  for n in "${NODES[@]}"; do
    $SSH "$n" "systemctl is-active --quiet etcd" \
      || die "[$label] etcd not active on $n"
    $SSH "$n" "$(etcdctl_for "$phase" "$n") endpoint health" >/dev/null 2>&1 \
      || die "[$label] endpoint health failed on $n"
  done
  # A write needs quorum + a leader — the real liveness signal.
  $SSH s1 "$(etcdctl_for "$phase" s1) put /rehearse/$label ok" >/dev/null 2>&1 \
    || die "[$label] quorum write failed"
  ok "[$label] all 3 nodes active, endpoints healthy, quorum write OK"
}

# Show the peer URLs in the membership DB so the http->https flip is visible.
show_members() {  # $1=phase
  local phase=$1
  echo "   membership (name -> peerURLs):"
  $SSH s1 "$(etcdctl_for "$phase" s1) member list" 2>/dev/null \
    | awk -F', ' '{printf "     %s -> %s\n", $3, $4}'
}

run_phase() {  # $1=phase
  local phase=$1
  log "PHASE: $phase  (rolling one node at a time)"
  for n in "${NODES[@]}"; do
    echo "   -> rolling $n to $phase"
    $PLAYBOOK etcd-roll.yml -e "etcd_tls_phase=$phase" --limit "$n" >/dev/null \
      || die "[$phase] etcd-roll failed on $n"
    # Quorum must hold after EACH node, while the others are a phase behind.
    health_check "$phase" "$phase/$n"
  done
  echo "   -> updating etcd clients (etcd-client.env) for $phase"
  $PLAYBOOK etcd-clients.yml -e "etcd_tls_phase=$phase" >/dev/null \
    || die "[$phase] etcd-clients failed"
  show_members "$phase"
}

bootstrap_plaintext() {
  log "BOOTSTRAP: wiping etcd -> fresh plaintext 3-node cluster"
  for n in "${NODES[@]}"; do
    $SSH "$n" "systemctl stop etcd 2>/dev/null || true"
  done
  sleep 2
  for n in "${NODES[@]}"; do
    # Targeted wipe of etcd's own state only (dev containers, ephemeral).
    $SSH "$n" "rm -rf /var/lib/etcd/member /var/lib/etcd/wal"
  done
  $PLAYBOOK etcd-roll.yml -e "etcd_tls_phase=off" >/dev/null \
    || die "plaintext bootstrap failed"
  $PLAYBOOK etcd-clients.yml -e "etcd_tls_phase=off" >/dev/null \
    || die "client bootstrap failed"
  health_check off bootstrap
  show_members off
}

assert_tls_only() {
  log "FINAL ASSERTIONS: TLS enforced, plaintext gone, cert required"
  # Plaintext client port must be closed.
  if $SSH s1 "etcdctl --endpoints http://${IP[s1]}:2379 --command-timeout=3s endpoint health" >/dev/null 2>&1; then
    die "plaintext :2379 still answering after enforce"
  fi
  ok "plaintext :2379 refused"
  # TLS port without a client cert must be rejected (client-cert-auth).
  if $SSH s1 "etcdctl --endpoints https://${IP[s1]}:2381 --cacert $TLSDIR/ca.crt --command-timeout=3s endpoint health" >/dev/null 2>&1; then
    die "TLS :2381 accepted a connection with no client cert after enforce"
  fi
  ok "TLS :2381 without client cert refused"
  # With a cert, it works.
  $SSH s1 "etcdctl --endpoints https://${IP[s1]}:2381 $CERTS endpoint health" >/dev/null 2>&1 \
    || die "TLS :2381 with client cert failed"
  ok "TLS :2381 with client cert healthy"
}

main() {
  [[ "${1:-}" == "--from-here" ]] || bootstrap_plaintext
  run_phase listen
  run_phase connect
  run_phase enforce
  assert_tls_only
  log "REHEARSAL COMPLETE — plaintext -> mTLS with no quorum gap"
}

main "$@"
