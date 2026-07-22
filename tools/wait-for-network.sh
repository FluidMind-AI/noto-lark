#!/bin/bash
# Block until this Mac actually has outbound connectivity, or until a
# timeout. The nightly jobs fire at 03:xx, when the network stack is
# frequently not up yet — every outbound call then fails with
# "Errno 49 Can't assign requested address" and the whole run dies
# (2026-07-15 ops review: this ate an entire nightly comb). Called at
# the top of network-dependent jobs so they WAIT for the dial tone
# instead of failing the instant the alarm goes off.
#
# Usage:  tools/wait-for-network.sh [max_wait_seconds]   (default 600)
# Exit:   0 as soon as a dependency host is reachable;
#         1 if still offline after max_wait (caller proceeds anyway,
#           and #1's push-guard / the next nightly self-heal the rest).
set -uo pipefail

MAX=${1:-600}          # give the network up to 10 min to come up
INTERVAL=15            # recheck cadence (seconds)
# Any ONE of these answering on 443 means we're online. Covers both
# Lark endpoints Noto uses (International + the tenant-token host some
# tools hit) plus GitHub for the backup push.
HOSTS=(github.com open.larksuite.com open.feishu.cn)

reachable() {
  local h
  for h in "${HOSTS[@]}"; do
    # curl exits 0 on ANY HTTP response (even 404) = network is up;
    # non-zero on connect/resolve failure = still offline.
    if curl -sS -o /dev/null --max-time 5 "https://$h" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

waited=0
while ! reachable; do
  if [ "$waited" -ge "$MAX" ]; then
    echo "wait-for-network: still offline after ${MAX}s — proceeding anyway" >&2
    exit 1
  fi
  sleep "$INTERVAL"
  waited=$((waited + INTERVAL))
done

[ "$waited" -gt 0 ] && echo "wait-for-network: online after ${waited}s" >&2
exit 0
