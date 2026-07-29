#!/bin/bash
# Daily health self-check (launchd com.noto.healthcheck, 07:30 SGT).
# The watchdog is itself watched: any crash of the check DMs
# engineering — a silent watchdog is the disease this cures.
set -uo pipefail
LOLABOT_HOME="${LOLABOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$LOLABOT_HOME"; [ -d .venv ] && source .venv/bin/activate
while IFS='=' read -r k _; do case "$k" in CLAUDE*|ANTHROPIC_*) unset "$k" 2>/dev/null||true;; esac; done < <(env)
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
LOG="lark/health-check.log"; mkdir -p lark
[ -x tools/wait-for-network.sh ] && tools/wait-for-network.sh 600 >>"$LOG" 2>&1 || true
if ! python3 tools/health_check.py >>"$LOG" 2>&1; then
  RC=$?
  if [ "$RC" -ge 2 ]; then   # crash (not the rc=1 some-checks-failed exit)
    python3 - <<'PY' >>"$LOG" 2>&1 || true
import sys; sys.path.insert(0, 'tools')
from engineering_notify import send
send("🚨 health-check runner CRASHED — see lark/health-check.log")
PY
  fi
fi
