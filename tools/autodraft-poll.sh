#!/bin/bash
# Auto-draft poller (com.noto.autodraft, every 15 min). See email_autodraft.py.
set -uo pipefail
LOLABOT_HOME="${LOLABOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$LOLABOT_HOME"; [ -d .venv ] && source .venv/bin/activate
while IFS='=' read -r k _; do case "$k" in CLAUDE*|ANTHROPIC_*) unset "$k" 2>/dev/null||true;; esac; done < <(env)
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
# mail users come from config (mail.users in lolabot.yaml)
USERS=$(python3 -c "import sys; sys.path.insert(0,'tools'); from mail_store import MAILBOXES; print(' '.join(MAILBOXES.keys()))" 2>/dev/null)
LOG="lark/autodraft.log"; mkdir -p lark
# fresh mail first (incremental, fast), then draft
for u in $USERS; do
  python3 tools/mail_store.py sync "$u" --label INBOX >> "$LOG" 2>&1 || true
done
echo "[$(date '+%F %T')] $(python3 tools/email_autodraft.py run 2>>"$LOG")" >> "$LOG"

# post-send delivery verification (blank-send guarantee layer)
python3 -c "import sys;sys.path.insert(0,'tools');import autodraft_card;autodraft_card.verify_recent_sends()" >> "$LOG" 2>&1 || true
