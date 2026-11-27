# CyberDefency — four-node release acceptance and control-plane integration

**Status: not deployed and not ready for paid service.** Four new VPS: RU-1→NL-1, RU-2→NL-2, optional direct NL routes. Retired DE/RU IPs and their old instructions are not applicable.

Use the current [recovery/cutover runbook](FOUR_NODE_MIGRATION_2026-11-20.md)
and [security boundaries](SUBSCRIPTION_SECURITY_BOUNDARY.md). The release gate
requires verified recovery mode/source, single-writer payment cutover,
post-write rollback, raw subscription isolation, and measured two-exit quota,
revocation and monitoring behavior. Use a fresh schema-2 evidence template.

## Seven workstreams: code versus deployment

| Workstream | Available or prepared code | What still requires operator access |
|---|---|---|
| Bot/subscriptions | Per-user Hiddify UUIDs, filtered subscriptions, UUID recovery after a lost panel response | Confirm account lifecycle and aggregate quota enforcement on both NL exits |
| Failover | Four distinct staging routes, tested sharding | Client-specific automatic failover on several real devices; HAProxy uses fixed backends |
| Monitoring | Panel health in the apps and a separate Xray SOCKS egress probe | Deploy probes from independent networks, verify DNS behavior and delivery of alert and recovery |
| Security | Secret scanner, redacted diagnostics, protected bundle | Verify all SSH host keys, rotate exposed credentials, review firewalls |
| Backup | Encrypted scripts; backup status no longer claims offsite dispatch when disabled | Test SQLCipher + Hiddify restore from offsite copy, keep decryption key separately |
| Payments/website | Existing YooKassa/Stars/USDT code, PWA/TMA and their tests | Verify real provider accounts, webhook signatures, offer, tariff quotas and reconciliation without blindly creating payments |
| Deployment/rollback | Offline generator, read-only preflight and release gate | Apply node by node after port/OS checks, test rollback and preserve encrypted customer backup |

## Critical commercial blocker: static demo credentials

The standalone four-node generator contains **one test VLESS UUID per NL
node**. This is for technical staging only. These are **not subscriber
accounts**. Reusing static credentials across paying users would break
per-customer revocation, quotas, expiry and bans.

The current bot provisions its own per-customer Hiddify account; it does
**not** automatically add those accounts to two unrelated standalone Xray
servers. Do not put staging links into live subscriptions or payment
receipts. Design and prove a single authoritative customer registry with
per-user provisioning/revocation on both NL exits before going live.

## Candidate control plane for staging

Use Hiddify Manager on NL-1 as the parent and NL-2 as its child, running
the same reviewed release on both. Keep the bot pointed at only the parent
admin API. This reuses the existing per-user UUID, subscription, expiry and
billing flows. Hiddify documents user sync and aggregate traffic reporting.
Its nodes can serve their last known state while disconnected from the parent,
so an accepted parent mutation does not establish data-plane revocation.
The staging bundle's standalone Xray configs remain useful for route testing.
For the managed topology the panel owns the Xray configuration and keys.

- Use a TLS-protected management hostname and restrict panel administration
  separately from the public subscription endpoint. Give the bot its own
  admin credential, with only the permissions already required for provisioning.
  Verify both nodes can authenticate to each other and that raw subscriptions
  cannot bypass the configured segment policy.
- Register each exit's own direct and paired relay addresses for the
  VLESS REALITY profiles. Check the addresses, ports, flow, SNI and key
  pairing in the actual subscription from the pinned panel version. Keep each
  RU HAProxy backend fixed to the corresponding NL listener. Check ownership
  of port 443 for each service and any additional ports required by TLS
  validation, panel synchronization or the bot before writing configs.
- With two synthetic users and isolated bot data, exercise trial/start, a test
  renewal, usage through each exit, aggregate quota crossing, expiry, suspend,
  resume and UUID rotation. Each user's profiles must connect only with that
  user's active credential. After rotation, test the old and new credential
  in fresh connections and check already-open sessions on both exits. Confirm
  the old public subscription URL no longer returns credentials.
- Repeat disable and rotation with the child isolated from its parent.
  Measure how long old credentials and sessions can keep forwarding and
  test the operator's procedure to remove the stale exit from service.
  Treat unbounded stale access or unbounded quota enforcement lag as a
  blocker until the owner has an enforceable policy and repeatable evidence.
- Back up the parent user/usage database and the configuration and key material
  needed to recover each node, as well as the bot database. Rehearse restoring
  all components in an isolated network, with outbound Telegram/payment
  integrations disabled, before following the single-writer cutover runbook.

[Hiddify multi-node guide](https://hiddify.com/manager/configuration-and-advanced-settings/Multi-server-with-nodes-on-HiddifyManager/) ·
[Hiddify REALITY guide](https://hiddify.com/manager/configuration-and-advanced-settings/How-to-use-Reality-on-Hiddify/)

Sharding one RU+NL pair to each segment isolates entry hosts but leaves
the subscriber without the other RU+NL backup pair. Showing all four routes
enables manual choice but exposes the four host addresses. A client list
does not, by itself, provide automatic failover. Confirm selected policy
and tested client app behavior before asserting redundancy.

## Offline subscription policy helper

Run after obtaining a trusted private four-node inventory, from the
repository root. Do not upload filled inventory or generated links:

    python3 -m deploy.four_node.plan_subscriptions --inventory /private/inventory.json --policy all-routes

Alternative for intentionally restricted address disclosure:

    python3 -m deploy.four_node.plan_subscriptions --inventory /private/inventory.json --policy isolated-pairs

The tool prints a **proposal only**. Both policies use the bot's filtered
subscription endpoint with strict host allowlists. All-routes assigns a
single segment containing the four approved entry hosts. The upstream
per-user Hiddify subscription must contain the corresponding profiles.
Isolated-pairs assigns two segments with one pair each. Keep the upstream
subscription reachable by the bot and block direct access by subscribers.
Verify client fallback, per-user provisioning on both NL exits and monitoring.

## Data-plane monitoring

Configure four dedicated Xray client profiles on an independent probe host.
Bind each profile to its own loopback-only SOCKS port and route to the VPN
entry. Keep the profiles private and use synthetic credentials with no billing
privileges or customer data. Use a controlled HTTPS endpoint whose body is the
source IP it sees. The expected IP is the paired NL exit's observed egress.

    python3 -m deploy.four_node.probe --route ru1 --socks-port 10871 --endpoint https://health.example.org/egress --expected-egress 192.0.2.10

Schedule one probe per route every minute. Pass `--textfile-dir` with the
directory configured in node_exporter's textfile collector. The probe user
needs write access and the collector needs read access. Each invocation
publishes a complete sample, including failed egress checks. Use
the `vpn-egress-probes` Prometheus scrape job for these targets and load
[`alerts.yml`](../deploy/four_node/alerts.yml) through `rule_files`. Every
target in that job is expected to check all four routes. Establish a separate
probe from a Russian access network and verify Alertmanager notification
and recovery while deliberately interrupting a staging route. A successful probe
demonstrates HTTPS egress via the selected SOCKS listener. Verify each
listener's Xray outbound and test DNS and real client failover separately.

## Evidence and offline release gate

- The owner confirmed TCP port 22 was reachable on four VPS.
- RU-1 refused SSH authentication by the tested key/password.
- There is no independently verified installation, subscription restore,
  client handshake from Russia, live billing or credential rotation.

Create a safe all-false evidence template by running:

    python3 deploy/four_node/release_gate.py --print-template

Keep any filled copy **outside this public repository**, with no private
keys or raw logs. Check release readiness:

    python3 deploy/four_node/release_gate.py --evidence /private/path/evidence.json

The output is BLOCKED until all checks are explicitly and recently
confirmed. A READY result is **self-attested**, not independently
verified; supporting logs and live client tests remain mandatory.
This script never accesses a VPS or changes infrastructure.

## Deployment order once SSH is authenticated

1. Verify host fingerprints with VPS provider; inspect OS, firewall, disk,
   memory and listeners, without changing existing services.
2. Test NL-1 REALITY using a non-paying account and real client.
3. Test NL-2 with independent keys; implement and verify per-customer
   provisioning, quotas and revocation on both exits.
4. Add RU-1→NL-1 and RU-2→NL-2 TCP passthrough; check the authenticated
   client tunnel from multiple Russian access networks.
5. Validate client selection, failover, TLS target, DNS leaks and egress IP.
6. Restore bot and billing from independently tested backups, rotate
   exposed secrets, validate provider signatures and reconciliation.
7. Verify offsite backup, encrypted restore, rollback and alerts before
   allowing paid users to connect.

No architecture or offline test guarantees immunity to VPN blocking.
