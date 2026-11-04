#!/usr/bin/env bash
set -euo pipefail
if (( EUID == 0 )); then
    exec /usr/bin/python3 /var/lib/warp-dual-test/manager.py off "$@"
fi
exec sudo -- /usr/bin/python3 /var/lib/warp-dual-test/manager.py off "$@"
