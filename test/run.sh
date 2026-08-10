#!/usr/bin/env bash
# Run the full claude-profile test suite: python core + zsh layer.
# Usage: test/run.sh          (bats/zsh portion is skipped if bats is missing)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== python unit tests =="
python3 "$here/test_claude_profile.py"

echo
if ! command -v bats >/dev/null 2>&1; then
  echo "== zsh layer: SKIPPED (bats not found — brew install bats-core / apt install bats) =="
  exit 0
fi
if ! command -v zsh >/dev/null 2>&1; then
  echo "== zsh layer: SKIPPED (zsh not found) =="
  exit 0
fi
echo "== zsh layer (bats) =="
bats "$here/picker.bats" "$here/wrapper.bats"
