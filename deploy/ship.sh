#!/usr/bin/env bash
# Build on the Mac, run on the Pi. Everything goes over SSH, so the Pi needs no access to GitHub or to an
# image registry, and holds no credentials for either.
#
#   deploy/ship.sh app       build the image, send it, restart the app   (the default)
#   deploy/ship.sh site      add dealwatch.home.arpa to the shared proxy and reload it
#   deploy/ship.sh backups   copy the Pi's nightly backups to backups/pi/ on this Mac
#   deploy/ship.sh status    what is running, disk, memory, temperature
#
# The Pi is reached as "pi-apps" (an entry in ~/.ssh/config). Another name: PI_HOST=name.
# The proxy itself belongs to ENDURANCE (its deploy/proxy). See docs/raspberry-pi.md.
set -euo pipefail

HOST="${PI_HOST:-pi-apps}"
cd "$(dirname "$0")/.."

# Keep Finder metadata (._ files, extended attributes) out of what is sent.
export COPYFILE_DISABLE=1
tar() { command tar --no-xattrs "$@"; }

case "${1:-app}" in
app)
    rev="$(git rev-parse --short HEAD)"
    git diff --quiet HEAD || rev="$rev-modified"
    docker build --platform linux/arm64 \
        --label "org.opencontainers.image.revision=$rev" -t dealwatch:latest .
    echo "Sending the image to $HOST (a few minutes the first time) ..."
    docker save dealwatch:latest | gzip | ssh "$HOST" 'gunzip | docker load'
    tar -C deploy/dealwatch -cf - compose.yaml make-env.sh | ssh "$HOST" 'tar -C /srv/dealwatch -xf -'
    # shellcheck disable=SC2016
    ssh "$HOST" 'cd /srv/dealwatch \
        && { [ -f .env ] || { echo "No .env yet. Run: ssh -t '"$HOST"' /srv/dealwatch/make-env.sh"; exit 1; }; } \
        && docker compose up -d --wait \
        && docker image prune -f >/dev/null'
    echo "Dealwatch $rev is running."
    ;;
site)
    # One file next to the other apps' sites; a reload does not interrupt them.
    ssh "$HOST" 'cat > /srv/proxy/conf/sites/dealwatch.caddy' < deploy/proxy/dealwatch.caddy
    ssh "$HOST" 'cd /srv/proxy && docker compose exec -T caddy caddy reload --force --config /etc/caddy/Caddyfile'
    echo "Proxy serves dealwatch.home.arpa."
    ;;
backups)
    mkdir -p backups/pi
    rsync -a "$HOST:/srv/dealwatch/backups/" backups/pi/
    echo "Newest copy: $(ls -1 backups/pi | tail -1)"
    ;;
status)
    # shellcheck disable=SC2016
    ssh "$HOST" 'docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Label \"org.opencontainers.image.revision\"}}" \
        && echo && df -h / | tail -1 | awk "{print \"disk: \" \$3 \" used of \" \$2 \" (\" \$5 \")\"}" \
        && free -m | awk "/^Mem/ {print \"memory: \" \$3 \" MB used of \" \$2 \" MB\"}" \
        && { vcgencmd measure_temp 2>/dev/null || true; } \
        && du -sh /srv/dealwatch/data /srv/dealwatch/backups 2>/dev/null'
    ;;
*)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
