#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == --help ]]; then
    echo 'Usage: ./install.sh'
    echo 'Installs the runtime and warp-on, warp-off, warp-speed commands.'
    echo 'For packaging checks only: DESTDIR=/absolute/staging/path ./install.sh'
    exit 0
fi
if (( $# )); then
    echo 'Unknown argument. Use --help.' >&2
    exit 2
fi

stage=${DESTDIR:-}
if [[ -n $stage && $stage != /* ]]; then
    echo 'DESTDIR must be an absolute path.' >&2
    exit 2
fi
if [[ -z $stage ]] && (( EUID != 0 )); then
    exec sudo -- "$0" "$@"
fi

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Retain runtime names so upgrades can control an already running setup.
runtime_dir="$stage/var/lib/warp-dual-test"
bin_dir="$stage/usr/local/bin"

if [[ -z $stage ]]; then
    for executable in python3 nmcli ip iw nft socat firewall-cmd systemd-run warp-cli warp-svc flock; do
        if ! command -v "$executable" >/dev/null; then
            echo "Missing required command: $executable" >&2
            exit 1
        fi
    done
fi

install -d -m 755 "$runtime_dir" "$bin_dir" "$runtime_dir/docs"
exec 9>"$runtime_dir/manager.lock"
flock -x 9
for module in engine.py manager.py traffic.py connectivity.py relay.py; do
    install -m 644 "$repo_dir/lib/$module" "$runtime_dir/$module"
done
install -m 755 "$repo_dir/warp-on.sh" "$bin_dir/warp-on"
install -m 755 "$repo_dir/warp-off.sh" "$bin_dir/warp-off"
install -m 755 "$repo_dir/watch-speed.py" "$bin_dir/warp-speed"
install -m 644 "$repo_dir/README.md" "$runtime_dir/README.md"
install -m 644 "$repo_dir/docs/architecture.md" "$runtime_dir/docs/architecture.md"

if [[ -n $stage ]]; then
    echo "Package staged under $stage"
else
    echo 'Installed. Run warp-on to choose paths and Wi-Fi, warp-off to restore, or warp-speed to monitor.'
    echo 'Installation does not restart networking or enable anything at boot.'
fi
