#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
if [[ $(id -u) -eq 0 && -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
    exec sudo -u "$SUDO_USER" -- bash "$ROOT/update.sh"
fi
checkout_status=$(git status --porcelain)
if [[ -n $checkout_status ]]; then
    echo 'Checkout has local changes. Commit or stash them before updating.' >&2
    exit 1
fi
# Refuse merges and divergent branches; use this checkout's configured upstream.
git pull --ff-only
exec bash "$ROOT/install.sh"
