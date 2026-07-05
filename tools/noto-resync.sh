#!/bin/bash
# Noto corpus re-sync — re-walk the Lark corpus so newly added
# documents / wiki pages / chats are picked up. Run nightly via launchd
# (deploy/com.noto.resync.plist), or by hand any time.
#
# Steps: re-walk the Drive tree + the wiki (refreshing artifacts),
# pull incremental chat history, then rebuild the doc index and the
# semantic/vector layers from artifacts.
set -uo pipefail

LOLABOT_HOME="${LOLABOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$LOLABOT_HOME"
[ -d .venv ] && source .venv/bin/activate

# launchd runs with a bare system PATH — the claude CLI (~/.local/bin)
# was unreachable, which silently killed every LLM step in this script
# for weeks (feedback analyzer, nugget extraction). Belt: user bins on
# PATH. Braces: noto_research._claude_bin() resolves the absolute path.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

ERRFILE=$(mktemp /tmp/noto-resync-errs.XXXXXX)
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  case "$*" in *"had errors"*) echo "$*" >> "$ERRFILE";; esac
}

# Read the corpus root tokens from lolabot.yaml.
read -r DRIVE WIKI <<EOF
$(python3 - <<'PY'
import sys; sys.path.insert(0, "tools")
from config import load_config
c = load_config().get("corpus", {}) or {}
print(c.get("drive_root", ""), c.get("wiki_root", ""))
PY
)
EOF

log "resync start — drive=$DRIVE wiki=$WIKI"

if [ -n "$DRIVE" ]; then
  log "walking the Drive tree…"
  python3 tools/lark_sync.py ingest-drive "$DRIVE" \
    || log "drive ingest had errors"
fi
if [ -n "$WIKI" ]; then
  log "walking the wiki…"
  python3 tools/lark_sync.py ingest-wiki "$WIKI" \
    || log "wiki ingest had errors"
fi

log "ingesting incremental chat history into the corpus (group chats "\
"the bot is in, minus excluded; powers the team Q&A nugget kb)…"
python3 tools/chat_corpus.py sync-all || log "chat corpus sync had errors"

log "rebuilding doc index from refreshed artifacts…"
python3 tools/doc_index.py compact || log "doc index compact had errors"

log "refreshing semantic vector index (local embeddings over corpus)…"
python3 tools/embeddings.py build-all || log "embeddings build had errors"

log "contextualizing pending chat nuggets (adds corpus context notes "\
"so single-exchange Q&A pairs are grounded before they surface)…"
python3 -c "
import sys; sys.path.insert(0, 'tools')
import chat_nuggets
chat_nuggets.contextualize_pending(verbose=True)
" || log "nugget contextualize had errors"

log "backfilling answerer names on nuggets (resolves open_ids that "\
"arrived before the contact cache knew them)…"
python3 tools/chat_nuggets.py backfill-names \
  || log "nugget name backfill had errors"

log "synthesizing unconsumed feedback into derived lessons (Noto reads "\
"the evidence, classifies scope/durability, writes reasoning — review "\
"in the admin panel Lessons tab)…"
python3 tools/feedback_synthesis.py synthesize || log "feedback synthesis had errors"

# Memory-index maintenance — optional component; skip cleanly if the
# tool isn't installed.
if [ -f tools/memory_indexer.py ]; then
  log "memory-index maintenance (promote mature short-term memories "\
"to long-term)…"
  python3 tools/memory_indexer.py promote \
    || log "memory-index maintenance had errors"
fi

# Push-alert failed steps (2026-07 ops review #1: '|| log' failures
# were invisible for weeks). Grep THIS run's window of the log.
ERRS=$(wc -l < "$ERRFILE" 2>/dev/null | tr -d ' ' || echo 0)
rm -f "$ERRFILE"
if [ "${ERRS:-0}" -gt 0 ]; then
  python3 -c "
import sys; sys.path.insert(0, 'tools')
try:
    from engineering_notify import send
    send('🌙 nightly resync finished with ' + '$ERRS' + ' failed step(s) — check lark/resync.log')
except Exception as e:
    print('alert send failed:', e)
" || true
fi

# Optional data auto-commit + push — OFF by default. Set NOTO_AUTOPUSH=1
# if you want the nightly to persist index state to your remote.
if [ "${NOTO_AUTOPUSH:-0}" = "1" ]; then
  log "syncing repo to remote (data auto-commit + push)…"
  # Auto-commit ONLY the data state (indexes/ — gitignore shields the
  # secrets and cursor files); never sweep uncommitted code — a half-
  # finished tool edit must not land in a nightly commit. Then push
  # everything already committed. Guarded to main so a dev branch is
  # never pushed by the nightly. (Added 2026-07-05: the repo had
  # silently drifted 80 commits ahead of origin — disaster recovery
  # depends on this push.)
  BRANCH=$(git -C "$(pwd)" branch --show-current 2>/dev/null)
  if [ "$BRANCH" = "main" ]; then
    git add indexes/ 2>/dev/null || true
    if ! git diff --cached --quiet 2>/dev/null; then
      git commit -q -m "Nightly state refresh: index state ($(date '+%Y-%m-%d'))" \
        || log "nightly data commit had errors"
    fi
    git push origin main >> /dev/null 2>&1 || log "git push had errors"
  else
    log "on branch '$BRANCH' (not main) — skipping auto-push"
  fi
else
  log "NOTO_AUTOPUSH not set — skipping data auto-commit/push"
fi

log "resync done"
