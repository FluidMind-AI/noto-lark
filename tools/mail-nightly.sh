#!/bin/bash
# Noto mail nightly (com.noto.mailnightly, 01:30) — keeps the F4 email
# stack fresh: incremental mailbox sync → vectors → playbook mining.
# Mining is capped per night so the LLM cost stays bounded; the cursor
# makes it chew steadily through any backlog over successive nights.
set -uo pipefail
LOLABOT_HOME="${LOLABOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$LOLABOT_HOME"; [ -d .venv ] && source .venv/bin/activate
while IFS='=' read -r k _; do case "$k" in CLAUDE*|ANTHROPIC_*) unset "$k" 2>/dev/null||true;; esac; done < <(env)
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
# mail users come from config (mail.users in lolabot.yaml)
USERS=$(python3 -c "import sys; sys.path.insert(0,'tools'); from mail_store import MAILBOXES; print(' '.join(MAILBOXES.keys()))" 2>/dev/null)
LOG="lark/mail-nightly.log"; mkdir -p lark
[ -x tools/wait-for-network.sh ] && tools/wait-for-network.sh 600 >>"$LOG" 2>&1 || true
echo "[$(date '+%F %T')] mail nightly start" >> "$LOG"
for u in $USERS; do
  python3 tools/mail_store.py sync "$u"            >> "$LOG" 2>&1 || echo "sync $u had errors" >> "$LOG"
  python3 tools/mail_retrieval.py build-vectors "$u" >> "$LOG" 2>&1 || echo "vectors $u had errors" >> "$LOG"
  python3 tools/email_playbook.py mine "$u" --limit 200 >> "$LOG" 2>&1 || echo "mine $u had errors" >> "$LOG"
done
echo "[$(date '+%F %T')] mail nightly done" >> "$LOG"
