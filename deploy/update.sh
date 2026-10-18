#!/usr/bin/env bash
# Pull the latest code and reinstall. Database, users, agents and settings are kept.
#   cd ~/algodesk && sudo bash deploy/update.sh
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -c %U "$SRC")"
runuser -u "$OWNER" -- git -C "$SRC" pull --ff-only
exec bash "$SRC/deploy/install.sh" "$@"
