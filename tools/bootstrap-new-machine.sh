#!/bin/bash
# Bootstrap a fresh Mac for Noto. Assumes the git repo is already
# cloned to /Users/noto/noto-home. Idempotent — safe to re-run.
# See docs/disaster-recovery.md for the full recovery playbook.

set -uo pipefail

LOLABOT_HOME="/Users/noto/noto-home"
if [ "$(pwd)" != "$LOLABOT_HOME" ]; then
  echo "→ cd'ing to $LOLABOT_HOME"
  cd "$LOLABOT_HOME" || {
    echo "  ✗ Repo not found at $LOLABOT_HOME"
    echo "     Clone first: git clone <repo-url> $LOLABOT_HOME"
    exit 1
  }
fi

step() { echo; echo "━━━ $* ━━━"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ─── 1) Homebrew + core packages ─────────────────────────────
step "1/7  Homebrew + core packages"

if ! have brew; then
  warn "Homebrew missing. Install first:"
  echo '     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi
ok "Homebrew present"

for pkg in python@3.12 tailscale cloudflared jq sqlite3; do
  if brew list --formula --version "$pkg" >/dev/null 2>&1; then
    ok "$pkg already installed"
  else
    echo "  installing $pkg…"
    brew install "$pkg" >/dev/null
    ok "$pkg installed"
  fi
done

# uv — Python package manager
if have uv; then
  ok "uv already installed"
else
  echo "  installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed"
fi

# Claude CLI
if have claude; then
  ok "Claude CLI already installed"
else
  warn "Claude CLI not installed. Install manually:"
  echo "     https://docs.claude.com/en/docs/claude-code/quickstart"
  echo "     (needed for the bot's research subprocesses)"
fi

# ─── 2) Python venv ──────────────────────────────────────────
step "2/7  Python venv + deps"

if [ -d .venv ]; then
  ok ".venv already exists"
else
  echo "  creating .venv…"
  uv venv --python 3.12 >/dev/null
  ok ".venv created"
fi

echo "  installing/updating requirements…"
source .venv/bin/activate
uv pip install -r requirements.txt --quiet
ok "requirements installed"

# ─── 3) Credentials check ────────────────────────────────────
step "3/7  Credentials"

if [ -f brain/credentials.yaml ]; then
  ok "brain/credentials.yaml exists"
else
  warn "brain/credentials.yaml MISSING."
  if [ -f brain/credentials.yaml.example ]; then
    echo "     Template available at brain/credentials.yaml.example"
    echo "     Copy it and fill in real values before continuing:"
    echo "       cp brain/credentials.yaml.example brain/credentials.yaml"
    echo "       chmod 600 brain/credentials.yaml"
    echo "       # then edit with real values from Lark Developer Console"
  fi
  echo
  echo "  → Bootstrap can't continue without credentials. See:"
  echo "    docs/disaster-recovery.md (Section 4)"
  exit 1
fi

# ─── 4) OAuth tokens check ───────────────────────────────────
step "4/7  OAuth tokens"

for ident in operator noah; do
  case $ident in
    operator) F="lark/user_token.json" ;;
    noah)     F="lark/user_token_noah.json" ;;
  esac
  if [ -f "$F" ]; then
    ok "$ident token present at $F"
  else
    warn "$ident token MISSING at $F"
    echo "     Re-authorize with:"
    echo "       python tools/lark_oauth.py --identity $ident url"
    echo "     Then open the printed URL in a Noah-logged-in browser"
    echo "     and approve. See docs/disaster-recovery.md Section 5."
  fi
done

# ─── 5) Tailscale ────────────────────────────────────────────
step "5/7  Tailscale"

TS_STATUS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
[ -x "$TS_STATUS" ] || TS_STATUS="$(command -v tailscale || echo tailscale)"
if $TS_STATUS status --json 2>/dev/null | jq -r '.Self.DNSName' | grep -q .; then
  HOST=$($TS_STATUS status --json | jq -r '.Self.DNSName' | sed 's/\.$//')
  ok "Tailscale up as $HOST"
  # Check funnel
  if $TS_STATUS funnel status 2>&1 | grep -q "127.0.0.1:8088"; then
    ok "Funnel proxying to 127.0.0.1:8088"
  else
    warn "Funnel not proxying port 8088. Run:"
    echo "     tailscale funnel --https=443 127.0.0.1:8088"
  fi
  # Nudge if hostname doesn't match lolabot.yaml
  CFG_HOST=$(grep -E "^\s*funnel_host:" lolabot.yaml | awk -F'"' '{print $2}')
  if [ -n "$CFG_HOST" ] && [ "$CFG_HOST" != "$HOST" ]; then
    warn "lolabot.yaml lark.funnel_host is '$CFG_HOST' but Tailscale says '$HOST'"
    echo "     Update lolabot.yaml AND the Lark Console Request URL"
  fi
else
  warn "Tailscale not logged in. Run:"
  echo "     tailscale login"
  echo "     tailscale funnel --https=443 127.0.0.1:8088"
fi

# ─── 6) launchd jobs ─────────────────────────────────────────
step "6/7  launchd jobs"

INSTALL_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$INSTALL_DIR"

for plist in deploy/com.noto.*.plist; do
  [ -f "$plist" ] || continue
  BASE=$(basename "$plist")
  if [ -f "$INSTALL_DIR/$BASE" ]; then
    ok "$BASE already installed"
  else
    cp "$plist" "$INSTALL_DIR/$BASE"
    launchctl load -w "$INSTALL_DIR/$BASE" 2>/dev/null || true
    ok "$BASE installed + loaded"
  fi
done

echo
echo "  loaded jobs:"
launchctl list | grep com.noto | awk '{print "    " $1 "  " $3}'

# ─── 7) Smoke test ───────────────────────────────────────────
step "7/7  Smoke test"

sleep 3

if lsof -nP -iTCP:8088 -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
  ok "Bot listening on :8088"
else
  warn "Bot NOT listening. Check lark/bot.err.log"
fi

if [ -n "${HOST:-}" ]; then
  RC=$(curl -sS -X GET "https://$HOST/lark/webhook" -o /dev/null \
        -w "%{http_code}" --max-time 8 2>/dev/null || echo "ERR")
  if [ "$RC" = "200" ]; then
    ok "Public URL healthy: https://$HOST/lark/webhook → 200"
  else
    warn "Public URL returned $RC (expected 200)"
  fi
fi

echo
echo "━━━ Bootstrap complete ━━━"
echo
echo "Next: DM the bot 'ping' in Lark. If it doesn't reply, walk"
echo "docs/disaster-recovery.md Section 7 (verify + smoke test)."
