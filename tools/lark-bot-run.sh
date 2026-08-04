#!/bin/bash
# Lark bot runner — asserts the Tailscale Funnel then serves the
# webhook. Used as the launchd ProgramArguments target (KeepAlive).
#
# Profile-aware: with NOTO_BOT_PROFILE unset this runs the primary bot
# (:8088 behind funnel :443) exactly as always. With
# NOTO_BOT_PROFILE=mail it runs the dedicated mail bot on its own port
# behind its own funnel HTTPS port — same stable hostname.
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

read -r PORT HTTPS_PORT <<< "$(python3 - <<'PY'
import sys; sys.path.insert(0,'tools')
from config import load_config, profile_config
listen = (profile_config().get('webhook_listen')
          or load_config().get('lark',{}).get('webhook_listen','127.0.0.1:8088'))
print(listen.split(':')[1], profile_config().get('funnel_https_port') or 443)
PY
)"

# Re-assert the funnel (idempotent; safe if already running; scoped to
# THIS profile's HTTPS port — it never touches the other bot's mapping).
# Non-fatal if tailscale is absent so the bot can still run on a public
# host. The CLI isn't always on launchd's PATH — fall back to the app
# bundle binary.
TAILSCALE="$(command -v tailscale || true)"
[ -z "$TAILSCALE" ] && [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ] && \
  TAILSCALE="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
if [ -n "$TAILSCALE" ]; then
  "$TAILSCALE" funnel --bg "--https=${HTTPS_PORT}" "127.0.0.1:${PORT}" || \
    echo "[lark-bot-run] warning: could not assert tailscale funnel" >&2
fi

# Privacy gate before serving (also enforced in serve()).
python3 tools/lark_bot.py assert-safe
exec python3 tools/lark_bot.py serve
