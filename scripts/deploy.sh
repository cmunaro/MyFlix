#!/usr/bin/env bash
# Privileged Raspberry Pi OS/systemd deployment, shared by install and update.
set -euo pipefail
umask 022
SOURCE=$1
PREFIX=/opt/myflix
SERVICE=/etc/systemd/system/myflix.service
RELEASE=
SWITCHED=0
STOPPED=0
PREVIOUS=

fail() { echo "MyFlix: $*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'Deployment needs root (use install.sh).'
[[ $(uname -s) == Linux && $(getconf LONG_BIT) == 64 ]] || fail 'Use 64-bit Raspberry Pi OS (or Debian with systemd).'
[[ -d /run/systemd/system ]] || fail 'A running systemd system is required.'
command -v apt-get >/dev/null || fail 'This installer requires a Debian-based OS with apt-get.'
command -v git >/dev/null || fail 'Install Git before cloning this project.'
cd "$SOURCE"
checkout_status=$(git -c safe.directory="$SOURCE" status --porcelain)
[[ -z $checkout_status ]] || fail 'Commit or stash checkout changes before installing.'
# A legacy in-place installation needs manual migration, never overwrite it.
[[ ! -e $PREFIX/myflix ]] || fail 'Existing in-place /opt/myflix installation found; migrate it before using this installer.'

# Serialize deployments, including package and tool installation.
exec 9>/run/lock/myflix-deploy.lock
flock -n 9 || fail 'Another MyFlix deployment is running.'

cleanup() {
    local result=$?
    trap - EXIT
    set +e
    if (( result != 0 )); then
        if (( SWITCHED )); then
            systemctl stop myflix || true
            if [[ -n $PREVIOUS ]]; then
                ln -s "$PREVIOUS" "$RELEASE/rollback-link"
                mv -Tf "$RELEASE/rollback-link" "$PREFIX/current"
            else
                rm -f "$PREFIX/current"
            fi
        fi
        if (( STOPPED )) && [[ -n $PREVIOUS ]]; then
            systemctl start myflix || true
        fi
        echo 'MyFlix deployment failed. Check: sudo journalctl -u myflix -n 80' >&2
        # Keep failed releases for diagnosis; no application state is removed.
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

apt-get update
apt-get install -y ca-certificates curl git xz-utils build-essential pkg-config libffi-dev libssl-dev tesseract-ocr tesseract-ocr-eng
install -d -m 0755 "$PREFIX" "$PREFIX/releases" "$PREFIX/.mise/bin"
export MISE_DATA_DIR="$PREFIX/.mise-data"
export MISE_CONFIG_DIR="$PREFIX/.mise-config"
export MISE_CACHE_DIR="$PREFIX/.mise-cache"
MISE="$PREFIX/.mise/bin/mise"
if [[ ! -x $MISE ]]; then
    installer=$(mktemp)
    curl --fail --silent --show-error --location https://mise.run -o "$installer"
    MISE_INSTALL_PATH="$MISE" sh "$installer"
    rm -f "$installer"
fi

getent group media >/dev/null || groupadd --system media
if ! id myflix >/dev/null 2>&1; then
    useradd --system --gid media --home-dir /var/lib/myflix --no-create-home --shell /usr/sbin/nologin myflix
fi
usermod -a -G media myflix
# Do not change permissions/ownership of an existing media mount or its contents.
if [[ ! -e /srv/media/library ]]; then
    install -d -o myflix -g media -m 2750 /srv/media/library
fi
if [[ ! -e /etc/myflix.env ]]; then
    install -m 0600 "$SOURCE/deploy/myflix.env.example" /etc/myflix.env
fi

RELEASE=$(mktemp -d "$PREFIX/releases/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
chmod 0755 "$RELEASE"
# Only committed application files: no local media, environments, or secrets.
git -c safe.directory="$SOURCE" archive HEAD | tar -x -C "$RELEASE"
cd "$RELEASE"
"$MISE" trust
"$MISE" install
"$MISE" run setup
# systemd's ProtectHome requires tools outside /home and readable by the service.
runuser -u myflix -- "$RELEASE/.venv/bin/python" -c 'import flask, waitress, PIL'

if [[ -L $PREFIX/current ]]; then
    PREVIOUS=$(readlink "$PREFIX/current")
elif [[ -e $PREFIX/current ]]; then
    fail "$PREFIX/current must be a symlink managed by this installer."
fi
# Local unit overrides belong in systemctl edit myflix and survive this refresh.
install -m 0644 deploy/myflix.service "$SERVICE"
systemctl daemon-reload
if [[ -n $PREVIOUS ]] || systemctl is-active --quiet myflix; then
    systemctl stop myflix
    STOPPED=1
fi
ln -s "$RELEASE" "$RELEASE/current-link"
mv -Tf "$RELEASE/current-link" "$PREFIX/current"
SWITCHED=1
systemctl enable myflix
systemctl start myflix

# Probe using the configured address/port; pass the env file to a transient
# systemd unit so systemd itself parses its EnvironmentFile syntax.
systemd-run --quiet --wait --pipe --collect --unit=myflix-deploy-check \
    --property=EnvironmentFile=/etc/myflix.env \
    "$RELEASE/.venv/bin/python" - <<'PY'
import os
import time
import urllib.request

host = os.environ.get("MYFLIX_HOST", "0.0.0.0")
if host in ("0.0.0.0", "::"):
    host = "127.0.0.1" if host == "0.0.0.0" else "::1"
if ":" in host:
    host = "[" + host + "]"
url = "http://{}:{}/health".format(host, os.environ.get("MYFLIX_PORT", "8090"))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(60):
    try:
        with opener.open(url, timeout=2) as response:
            if response.status == 200:
                break
    except OSError:
        pass
    time.sleep(1)
else:
    raise SystemExit("MyFlix did not become healthy within the startup window")
PY
systemctl is-active --quiet myflix
SWITCHED=0
STOPPED=0
echo 'MyFlix installed and running. Open http://<raspberry-LAN-IP>:8090 (or your configured port).'
echo 'Choose /srv/media/library in Admin → Settings. Logs: sudo journalctl -u myflix -f'
echo "Release: $RELEASE"
