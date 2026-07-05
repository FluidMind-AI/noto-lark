#!/bin/bash
# Bundle the git-ignored critical files (Lark app creds + OAuth
# tokens + tunnel state) into a single password-protected zip you
# can stash somewhere off the Mac. Run monthly. See
# docs/disaster-recovery.md for the recovery playbook.

set -uo pipefail

LOLABOT_HOME="${LOLABOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$LOLABOT_HOME"

DEST="${1:-$HOME/Documents/noto-credentials-backup-$(date +%Y%m%d).zip}"

echo "→ Bundling critical secrets into $DEST"

# Fail-fast list of what we're backing up. Missing files are noted
# but don't abort — an operator running this before OAuth may not
# have the token files yet.
FILES=(
  "brain/credentials.yaml"
  "brain/eisenhower.md"
  "lark/user_token.json"
  "lark/user_token_noah.json"
  "lark/cloudflared-url.txt"
  "lark/state.json"
  "lolabot.yaml"
)

echo "  including (or noting-missing):"
for f in "${FILES[@]}"; do
  if [ -f "$f" ]; then
    printf "    ✓ %s\n" "$f"
  else
    printf "    ⚠ MISSING: %s\n" "$f"
  fi
done

echo
echo "→ Enter a password to encrypt the archive (leave blank for no encryption):"
read -rs PASSWORD
echo

if [ -n "$PASSWORD" ]; then
  zip -q -j -P "$PASSWORD" "$DEST" "${FILES[@]}" 2>/dev/null || true
  echo "  ✓ Encrypted zip written to $DEST"
  echo "  ⚠ Keep the password safe — losing it means losing the backup."
else
  zip -q -j "$DEST" "${FILES[@]}" 2>/dev/null || true
  echo "  ✓ Zip written to $DEST (NOT encrypted — treat with care)"
fi

echo
echo "→ Recommended: also back up indexes/ if you care about"
echo "  accumulated operational state:"
echo "    tar -czf $HOME/Documents/noto-indexes-$(date +%Y%m%d).tar.gz -C \"$LOLABOT_HOME\" indexes/"
