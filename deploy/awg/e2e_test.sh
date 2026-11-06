#!/usr/bin/env bash
# End-to-end check of the subscriber AmneziaWG server, run on any Docker host
# with /dev/net/tun. Nothing here touches existing containers or networks.
#
#   bash deploy/awg/e2e_test.sh [image]
#
# Topology (all inside Docker):
#   inet  198.51.100.0/24  stands in for the internet: server .2, client .3, web .10
#   botnet 172.30.99.0/24  stands in for the bot network: server .2, "bot" .5
# The client imports a peer created through the agent API, then:
#   1. reaches the web host through the tunnel (web sees the server's address);
#   2. cannot reach the agent, the "bot" or private networks through the tunnel;
#   3. loses access when the peer is disabled and regains it when enabled;
#   4. keeps access after a server restart (state persisted).
set -euo pipefail

IMAGE="${1:-cyberdefency-awg:test}"
P="awg-e2e-$$"
TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
PASS=0

cleanup() {
  docker rm -f "$P-server" "$P-client" "$P-web" "$P-bot" >/dev/null 2>&1 || true
  docker network rm "$P-inet" "$P-botnet" >/dev/null 2>&1 || true
  docker volume rm "$P-state" >/dev/null 2>&1 || true
}
trap cleanup EXIT
ok() { PASS=$((PASS + 1)); printf 'PASS %s\n' "$*"; }
die() { printf 'FAIL %s\n' "$*" >&2; docker logs "$P-server" 2>&1 | tail -20 >&2; exit 1; }

docker network create --subnet 198.51.100.0/24 "$P-inet" >/dev/null
docker network create --subnet 172.30.99.0/24 "$P-botnet" >/dev/null
docker volume create "$P-state" >/dev/null

docker run -d --name "$P-web" --network "$P-inet" --ip 198.51.100.10 \
  --entrypoint python3 "$IMAGE" -c '
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.client_address[0].encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("0.0.0.0", 8000), H).serve_forever()' >/dev/null
docker run -d --name "$P-bot" --network "$P-botnet" --ip 172.30.99.5 \
  --entrypoint python3 "$IMAGE" -m http.server 8080 >/dev/null

start_server() {
  docker create --name "$P-server" --cap-add NET_ADMIN --device /dev/net/tun \
    --sysctl net.ipv4.ip_forward=1 --network "$P-inet" --ip 198.51.100.2 \
    -e AWG_ENDPOINT_HOST=198.51.100.2 -e AWG_AGENT_TOKEN=${TOKEN} \
    -v "$P-state:/var/lib/awg" "$IMAGE" >/dev/null
  docker network connect --ip 172.30.99.2 "$P-botnet" "$P-server"
  docker start "$P-server" >/dev/null
  for _ in $(seq 1 30); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$P-server")" == healthy ]] && return 0
    sleep 1
  done
  die "server did not become healthy"
}

api() {  # api METHOD PATH [JSON]
  docker run --rm --network "$P-botnet" --entrypoint python3 "$IMAGE" -c '
import json, sys, urllib.request
method, path, token = sys.argv[1], sys.argv[2], sys.argv[3]
data = sys.argv[4].encode() if len(sys.argv) > 4 else None
req = urllib.request.Request("http://172.30.99.2:8090" + path, data=data, method=method,
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=10).read().decode())
except urllib.error.HTTPError as exc:
    print(json.dumps({"http_status": exc.code}))' "$1" "$2" "$TOKEN" "${3:-}"
}
json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

start_server
ok "server healthy"

[[ "$(api GET /v1/info | json "['port']")" == 39051 ]] || die "info"
docker run --rm --network "$P-botnet" --entrypoint python3 "$IMAGE" -c '
import urllib.request, urllib.error
try:
    urllib.request.urlopen("http://172.30.99.2:8090/v1/info", timeout=5)
except urllib.error.HTTPError as e:
    raise SystemExit(0 if e.code == 401 else 1)
raise SystemExit(1)' && ok "API refuses requests without the token" || die "API without token"

docker run -d --name "$P-client" --cap-add NET_ADMIN --device /dev/net/tun \
  --network "$P-inet" --ip 198.51.100.3 --entrypoint sleep "$IMAGE" infinity >/dev/null
PRIV="$(docker exec "$P-client" awg genkey)"
PUB="$(printf '%s\n' "$PRIV" | docker exec -i "$P-client" awg pubkey)"
PSK="$(docker exec "$P-client" awg genpsk)"
ADDR="$(api POST /v1/peers/upsert "{\"public_key\":\"$PUB\",\"preshared_key\":\"$PSK\",\"enabled\":true}" | json "['address']")"
INFO="$(api GET /v1/info)"
p() { printf '%s' "$INFO" | json "['params']['$1']"; }
SERVER_PUB="$(printf '%s' "$INFO" | json "['public_key']")"
docker exec -i "$P-client" sh -c 'cat > /run/c.conf' <<CONF
[Interface]
PrivateKey = $PRIV
Jc = $(p jc)
Jmin = $(p jmin)
Jmax = $(p jmax)
S1 = $(p s1)
S2 = $(p s2)
H1 = $(p h1)
H2 = $(p h2)
H3 = $(p h3)
H4 = $(p h4)

[Peer]
PublicKey = $SERVER_PUB
PresharedKey = $PSK
Endpoint = 198.51.100.2:39051
AllowedIPs = 198.51.100.10/32, 172.30.99.0/24
PersistentKeepalive = 5
CONF
docker exec "$P-client" sh -c "amneziawg-go awg0 && awg setconf awg0 /run/c.conf \
  && ip address add $ADDR/32 dev awg0 && ip link set dev awg0 mtu 1280 up \
  && ip route add 198.51.100.10/32 dev awg0 && ip route add 172.30.99.0/24 dev awg0"

fetch() { docker exec "$P-client" wget -qO- -T 4 "$1" 2>/dev/null; }
SEEN="$(fetch http://198.51.100.10:8000/ || true)"
[[ "$SEEN" == 198.51.100.2 ]] && ok "web reached through the tunnel (seen from $SEEN)" \
  || die "web via tunnel: got '$SEEN'"
fetch http://172.30.99.2:8090/healthz >/dev/null && die "agent reachable from the tunnel" \
  || ok "agent not reachable from the tunnel"
fetch http://172.30.99.5:8080/ >/dev/null && die "private network reachable" \
  || ok "private networks not reachable from the tunnel"

RX="$(api GET /v1/peers | json "['peers'][0]['rx_bytes']")"
((RX > 0)) && ok "traffic counted ($RX bytes received)" || die "no counters"

api POST /v1/peers/upsert "{\"public_key\":\"$PUB\",\"preshared_key\":\"$PSK\",\"enabled\":false}" >/dev/null
fetch http://198.51.100.10:8000/ >/dev/null && die "disabled peer still works" || ok "disabled peer is cut off"
api POST /v1/peers/upsert "{\"public_key\":\"$PUB\",\"preshared_key\":\"$PSK\",\"enabled\":true}" >/dev/null
sleep 2
[[ "$(fetch http://198.51.100.10:8000/ || true)" == 198.51.100.2 ]] && ok "re-enabled peer works" \
  || die "re-enabled peer"

docker rm -f "$P-server" >/dev/null
start_server
# WireGuard clients re-handshake within ~5-15 s after the server lost its session.
RESTORED=""
for _ in $(seq 1 10); do
  RESTORED="$(fetch http://198.51.100.10:8000/ || true)"
  [[ "$RESTORED" == 198.51.100.2 ]] && break
  sleep 3
done
[[ "$RESTORED" == 198.51.100.2 ]] && ok "peer restored after server restart" \
  || die "after restart"
[[ "$(api POST /v1/peers/delete "{\"public_key\":\"$PUB\"}" | json "['deleted']")" == True ]] \
  && ok "peer deleted" || die "delete"

printf 'AWG_E2E_OK %d checks\n' "$PASS"
