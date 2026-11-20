# CyberDefency — four-node release acceptance and control-plane integration

**Status: not deployed and not ready for paid service.** Four new VPS: RU-1→NL-1, RU-2→NL-2, optional direct NL routes. Retired DE/RU IPs and their old instructions are not applicable.

## Seven workstreams: code versus deployment

| Workstream | Available or prepared code | What still requires operator access |
|---|---|---|
| Bot/subscriptions | Per-user Hiddify UUIDs, filtered subscriptions; missing sharded subscriptions now fail closed | Confirm each customer is provisioned, disabled and revoked on **both** NL exits |
| Failover | Four distinct staging routes, tested sharding | Client-specific automatic failover on several real devices; HAProxy uses fixed backends |
| Monitoring | Panel-only health now labelled honestly in bot/API/PWA/TMA; existing mobile Termux probe | Actual REALITY handshake, egress, DNS leak and ISP-specific probe checks |
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

Sharding one RU+NL pair to each segment isolates entry hosts but leaves
the subscriber without the other RU+NL backup pair. Showing all four routes
enables manual choice but exposes the four host addresses. A client list
does not, by itself, provide automatic failover. Confirm selected policy
and tested client app behavior before asserting redundancy.

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
