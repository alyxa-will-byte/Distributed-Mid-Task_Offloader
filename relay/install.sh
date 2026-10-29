#!/usr/bin/env bash
# axr relay on the KVM 2, next to the Qi gateway and behind the same Caddy.
#   sudo bash install.sh relay.alyxrbytes.pro
# Needs: Docker, the live-stream install (/opt/live, Caddy), and a DNS A record for the name -> this server.
# Writes /opt/axr-relay (relay + admin key in .env, mode 600), keeps the device registry in /opt/axr-relay/data
# (back it up: losing it means re-pairing every device), starts it on 127.0.0.1:8790, adds a Caddy block. Safe to run again.
set -euo pipefail
DOMAIN="${1:-relay.alyxrbytes.pro}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR=/opt/axr-relay
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
command -v docker >/dev/null && docker compose version >/dev/null || { echo "Docker is missing: run the live-stream install first"; exit 1; }
[ -f /opt/live/Caddyfile ] || { echo "/opt/live (Caddy) not found: run the live-stream install first"; exit 1; }

mkdir -p "$DIR/data"; chmod 700 "$DIR/data"
cp "$SRC/relay.mjs" "$SRC/package.json" "$SRC/package-lock.json" "$DIR/"
cd "$DIR"
if [ ! -f .env ]; then (umask 077; printf 'RELAY_ADMIN_KEY=%s\nRELAY_PORT=8790\nRELAY_DATA=/app/data\nRELAY_HOST=127.0.0.1\n' "$(openssl rand -hex 32)" > .env); fi

cat > docker-compose.yml <<'YML'
services:
  relay:
    image: node:22-alpine
    restart: unless-stopped
    network_mode: host
    working_dir: /app
    env_file: .env
    volumes:
      - ./relay.mjs:/app/relay.mjs:ro
      - ./package.json:/app/package.json:ro
      - ./package-lock.json:/app/package-lock.json:ro
      - ./data:/app/data
      - modules:/app/node_modules
    command: ["sh", "-c", "npm ci --omit=dev --no-audit --no-fund && exec node relay.mjs serve"]
volumes:
  modules:
YML
docker compose up -d --force-recreate

if ! grep -q "^$DOMAIN" /opt/live/Caddyfile; then
  cat >> /opt/live/Caddyfile <<CADDY

$DOMAIN {
	reverse_proxy localhost:8790
}
CADDY
fi
(cd /opt/live && docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile)
sleep 3
echo
echo "axr relay is up at wss://$DOMAIN"
echo "Pair your laptop first (one-time code, valid 10 minutes):"
echo "  cd $DIR && docker compose exec relay node relay.mjs pair controller"
echo "Then on the laptop:  axr join <code>"
