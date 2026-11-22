# Four-node VLESS REALITY staging bundle

**Status: experimental, not deployed.** These files are for four *new* VPS,
not the retired DE/RU nodes. The repo is public. **Never commit** filled
inventory, generated configurations, UUIDs, X25519 keys or subscription links.

## Topology

| Public entry | Endpoint | Fixed backend | Keys in client profile |
|---|---|---|---|
| RU-1 TCP 443 | HAProxy TCP passthrough | NL-1 TCP 443 | NL-1 key pair |
| RU-2 TCP 443 | HAProxy TCP passthrough | NL-2 TCP 443 | NL-2 key pair |
| NL-1 TCP 443 | Xray REALITY direct | — | NL-1 key pair |
| NL-2 TCP 443 | Xray REALITY direct | — | NL-2 key pair |

**No cross-NL failover in HAProxy.** The two NL servers deliberately have
different keys and user IDs. Switching RU-1 to NL-2 would break the client
REALITY handshake. Instead, distribute four distinct client entries and
test profile selection/failover in the **specific client app**. Each RU
endpoint only uses TCP liveness checks; this does not prove an authentic
REALITY handshake or successful internet traffic.

## Prepare once, offline (requires trusted Xray CLI)

Use **Python 3.11** and a trusted recent **Xray CLI** capable of
`xray x25519`. Choose a REALITY target after checking its TLS behavior
from **both** NL locations. This generator intentionally **does not**
auto-select a cover domain or perform external TLS tests.

1. Create a **private** folder outside the Git repository (permissions 0700).
2. Create an `inventory.json` file in that folder, with the schema below.
   Replace every placeholder with the real public IPv4. The example values
   are not valid input and cannot be deployed.
3. Run:

```sh
python3 deploy/four_node/generate.py \
  --inventory /private-path/inventory.json \
  --output /private-path/four-node-generated \
  --xray /trusted-path/xray
```

Private inventory **format** (not a usable configuration):

```json
{
  "ru1": "<RU-1 public IPv4>",
  "ru2": "<RU-2 public IPv4>",
  "nl1": "<NL-1 public IPv4>",
  "nl2": "<NL-2 public IPv4>",
  "server_name": "<verified real TLS server name>",
  "target": "<same name>:443"
}
```

Output structure:

```text
four-node-generated/      # 0700; only outside the public repository
  nl1/config.json          # 0600; Xray REALITY private key + UUID
  nl1/compose.yaml
  nl2/config.json
  nl2/compose.yaml
  ru1/haproxy.cfg          # fixed NL-1 TCP forwarding
  ru1/compose.yaml
  ru2/haproxy.cfg          # fixed NL-2 TCP forwarding
  ru2/compose.yaml
  vless-links.txt          # sensitive client credentials; 0600
  README.txt
```

The official Xray container version is **26.11.9**, pinned with a GHCR image
digest. Review upstream security notices and client compatibility before
production. The generator requires the real `xray x25519` command, which
creates fresh independent keypairs; it never logs them. If Xray is missing,
the generator exits without deploying.

## First authenticated access / preparation (NOT performed)

Before running any install commands, independently identify all four VPS
using trusted **SSH host key fingerprints** from the provider. Check disk,
memory, existing services and port 443 with `preflight.sh`; it only reads.

Check REALITY target viability on each NL VPS using the same Xray version
(e.g. `xray tls ping <chosen-host>`) and inspect HTTP/2, certificate,
response size and routing. Avoid using an unsafe shared CDN target as
an unauthenticated forward-proxy destination. Ordinary TCP success is
insufficient.

Before starting Xray: verify Docker version, `docker compose config -q`,
`xray run -test -c /etc/xray/config.json` with the **same pinned image**,
firewall rules, and exclusive ownership of port 443. Never reset UFW/nftables
or blindly stop services. Ensure private config permissions on each VPS
allow the Xray container to read it.

Before starting RU relays: validate `haproxy -c -f haproxy.cfg`. Install
each config to the correct VPS only after verifying its role. Enable
only the required TCP 443 ingress, with NL egress firewall/rate limits
reviewed separately. Each RU folder includes a Compose service pinned to
an official HAProxy image digest. On an authorized staging host, apply the
single role's Compose project from its own private directory only after
validating the config and preserving the previous revision for rollback.

```sh
# Example for the authorized RU-1 staging host, from its ru1 directory.
docker compose -p cyberdefency-ru1 config --quiet
docker compose -p cyberdefency-ru1 run --rm --no-deps haproxy \
  haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg
docker compose -p cyberdefency-ru1 up -d
```

Verify the paired egress after applying. If that check fails, restore the
previous private files and run the same Compose project again with
`up -d --force-recreate`. On a first install, stop this project's service
using `docker compose -p cyberdefency-ru1 stop haproxy`. Check that a host
service or another Compose project does not already own TCP 443.

After installation, verify separately for **each** of the four entries:
REALITY authentication, external egress IP, DNS resolution/leaks,
client compatibility, TLS-target failure, throughput, and Russian carrier
reachability. Simulate NL-1 and RU-1 failures. Expect **manual** route
switching unless the chosen client is verified to support auto-failover.

## GitHub checks

The repository CI tests mapping of RU->NL, independence of keys, private
file permissions, inventory validation and output overwrite protection.
The four-node workflow also launches a private Docker network with two
HAProxy relays, two pinned Xray exits, an Xray client and a local TLS target.
The test checks HTTP egress through real VLESS REALITY sessions through each paired route,
rejects the wrong key and an unissued UUID, and stops NL-1 to verify the
other pair still carries traffic. The test uses synthetic data in CI.
The public VPS topology and real client failover require acceptance tests.

## Integration with the existing bot

This bundle generates VLESS URI lines, **not** a production subscription
server. Bot, payment gateway, existing customer data and Hiddify/Marzban
integration are intentionally unchanged. A source/DB migration and
rotation of previously exposed bot, SMTP and payment credentials must be
planned separately; never assume old database data exists on new VPS.
