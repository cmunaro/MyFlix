#!/usr/bin/env bash
# Run from any directory; the checkout can live anywhere.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ $(id -u) -eq 0 ]]; then
    exec bash "$ROOT/scripts/deploy.sh" "$ROOT"
fi
exec sudo bash "$ROOT/scripts/deploy.sh" "$ROOT"
