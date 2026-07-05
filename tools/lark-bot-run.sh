#!/bin/bash
# Noto Lark bot runner — asserts the Tailscale Funnel then serves the
# webhook. Used as the launchd ProgramArguments target (KeepAlive).
set -euo pipefail

LOLABOT_HOME="${LOLABOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$LOLABOT_HOME"
[ -d .venv ] && source .venv/bin/activate

# Scrub inherited Claude-Code session env. When the bot is (re)started
# from inside an interactive Claude Code session, vars like CLAUDECODE=1
# and CLAUDE_CODE_SESSION_ID leak in and change how the bot's nested
# `claude -p` subprocesses behave. The bot must always run its LLM
# calls as a clean standalone CLI.
while IFS='=' read -r k _; do
  case "$k" in CLAUDE*|ANTHROPIC_*) unset "$k" 2>/dev/null || true ;; esac
done < <(env)

PORT="$(python3 - <<'PY'
import sys; sys.path.insert(0,'tools')
from config import load_config
print(load_config().get('lark',{}).get('webhook_listen','127.0.0.1:8088').split(':')[1])
PY
)"

# Re-assert the funnel (idempotent; safe if already running). Non-fatal
# if tailscale is absent so the bot can still run on a public host.
if command -v tailscale >/dev/null 2>&1; then
  tailscale funnel --bg "--https=443" "127.0.0.1:${PORT}" || \
    echo "[lark-bot-run] warning: could not assert tailscale funnel" >&2
fi

# Privacy gate before serving (also enforced in serve()).
python3 tools/lark_bot.py assert-safe
exec python3 tools/lark_bot.py serve
