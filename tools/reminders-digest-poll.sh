#!/bin/bash
# Noto reminders digest — runs every 15 min via launchd.
# Sends each teammate their morning reminders DM once their LOCAL
# clock passes 08:00 (team spans 8 timezones, so this must tick all
# day; reminders_digest.py stamps per-person per-local-date, making
# reruns free). No-op while h2.reminders_enabled is off.
set -uo pipefail

LOLABOT_HOME="${LOLABOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$LOLABOT_HOME"
[ -d .venv ] && source .venv/bin/activate

# launchd's bare PATH fix (same as email-pipeline-poll.sh)
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

LOG="lark/reminders-digest.log"
mkdir -p lark
echo "[$(date '+%Y-%m-%d %H:%M:%S')] digest tick start" >> "$LOG"
python3 tools/reminders_digest.py post 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[$(date '+%Y-%m-%d %H:%M:%S')] digest tick done rc=$RC" >> "$LOG"
if [ "$RC" -ne 0 ]; then
  STAMP="lark/.reminders-digest-alerted"
  if [ ! -f "$STAMP" ] || [ -n "$(find "$STAMP" -mmin +360 2>/dev/null)" ]; then
    python3 -c "
import sys; sys.path.insert(0, 'tools')
try:
    from engineering_notify import send
    send('⏰ reminders digest tick FAILED (rc=$RC) — check lark/reminders-digest.log. This alert repeats at most every 6h.')
except Exception as e:
    print('alert send failed:', e)
" || true
    touch "$STAMP"
  fi
else
  rm -f "lark/.reminders-digest-alerted"
fi
exit "$RC"
