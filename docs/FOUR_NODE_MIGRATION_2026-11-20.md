# CyberDefency: migration checklist for four new VPS (2026-11-20)

> **Status: execution runbook, not executed.** This document does not confirm connectivity, provisioning, deployed services, or working VPN connections. Do not use the old DE/RU runbook for these VPS.

## Scope and source of truth

The owner has four newly provisioned machines: RU-1, RU-2, NL-1 and NL-2. Their addresses, login credentials, secrets, and other private deployment parameters must **not** be committed to this public repository. Keep a private inventory outside Git.

Prior hostnames/IPs in older documents, scripts and backups are **retired**. No production bot or customer database is assumed to exist on the new hosts until independently verified.

## Proposed topology (not yet deployed)

- NL-1: primary Xray VLESS/REALITY exit.
- NL-2: secondary Xray VLESS/REALITY exit.
- RU-1: TCP pass-through **only to NL-1**, preserving NL-1 REALITY keys.
- RU-2: TCP pass-through **only to NL-2**; both ingress routes require separate client profiles. Do not switch across independent REALITY keypairs.
- Control plane (bot, database, payments, subscriptions) remains **unassigned** until its backups, personal-data requirements, and secret rotation are reviewed.

**Failure domains:** Two machines at the same provider / ASN and two NL addresses in the same subnet are not independent of provider/network outage. A second provider/ASN is preferable for a real geographic failover design. TCP reachability or a process check does not prove a usable VPN connection through Russian ISP filtering.

**Security:** Do not decrypt customer traffic on ingress relay. Do not expose Xray management API or a panel publicly. Avoid exposing customer keys or traffic logs unnecessarily.

## Phase 0 — no-SSH work (can be done from a phone)

1. Review source repo and CI using GitHub; work on a branch/PR, never direct production.
2. Identify code and documentation still assuming the retired IPs, hosts, Hiddify/Marzban data, and static TLS targets.
3. Design a subscription format supporting independent inbound routes and per-node health status; do not pretend a client will automatically fail over without testing.
4. Inventory payment/account recovery and backup availability. Do not recreate customer subscriptions or mark payments confirmed based on old logs alone.
5. Plan credential rotation: an earlier Docker inspect export exposed sensitive environment variables, including bot, SMTP, and panel credentials. Treat exposed values as compromised; replace via the relevant provider control panels and private secret storage **before** restoring production.
6. Prepare tests for four distinct failure modes: NL-1 unavailable, RU-1 unavailable, TLS target unavailable, and traffic filtered by an ISP.

## Phase 1 — first contact (requires authenticated SSH or provider console)

Before starting this phase, record the owner-approved recovery mode below.
Source review and passing CI do not authorize changes on VPS, DNS or payments.

For each host, separately verify:

- SSH host key fingerprint **against a trusted provider channel** before connecting.
- OS/kernel/architecture, free disk/RAM, current routes, DNS, and listening ports.
- Firewall policy, existing services, Docker version, and container names.
- Public IP ownership/role and actual machine identity; never rely solely on a reported address.
- Whether port 443 is free; never stop an unknown listener.
- Host clock synchronized. Document each result and any disagreement with inventory.

**Stop condition:** If any SSH key or host identity is unexpected, stop. If the port 443 is occupied, do not deploy over it. Avoid global firewall resets and blind Docker cleanup.

## Phase 2 — staged provisioning (requires SSH + backups)

1. Generate independent per-node key material on a trusted staging host and transfer privately to NL-1; test Xray config syntax before starting. Never publish generated bundles to GitHub.
2. Conduct a real client handshake, egress-IP check, DNS-leak check and throughput sanity check. Verify selected REALITY cover target actually meets protocol requirements from the node's location.
3. Provision NL-2 separately with independent key material and client record.
4. Add RU-1→NL-1 and RU-2→NL-2 as fixed *TCP passthrough* paths, preserving end-to-end transport. Verify routing rules and actual authenticated handshakes; a basic TCP health probe does not validate REALITY.
5. Exercise profile switching or automatic failover **only in clients known to support it** using four independent connection entries; record test result, source network type and date. Keep direct NL routes as optional fallback.
6. Only after validated data restore, secrets rotation, legal review and backups, migrate bot, subscribers, billing and monitoring to a control-plane host.

## Release gates and rollback

- **Before any deployment:** complete and verified encrypted backup of relevant customer data (if it exists), known SSH recovery path, saved existing firewall, and maintenance/rollback plan.
- Deploy one node at a time; stage changes in a GitHub PR, review CI and scripts, then roll out by explicit operator action.
- Check healthy process **and** functioning user traffic. On failure, restore previous service/config; do not automatically wipe firewall or containers.
- Keep failover thresholds and monitoring probes documented, and monitor from at least one Russian access network and one external network.
- A single functioning tunnel is not proof of censorship resistance; no architecture is unblockable.

## Current evidence (as of 2026-11-20)

- User-provided TCP port test returned `True` for port 22 on each of four machines.
- SSH log for RU-1 showed public-key and password authentication rejected, followed by remote close. This does not demonstrate working administrative access.
- No validated production deployment or successful end-to-end test for these four nodes is recorded.
- The legacy repository documents refer to old DE/RU infrastructure; don't execute legacy runbooks against the new VPS.

## Recovery mode decision — before provisioning customer services

Keep the decision and supporting evidence privately, with an accountable operator,
reviewer, timestamp and the exact release commit/image digests. Do not fill unknown
fields from the historic DE/RU instructions.

| Mode | Required evidence | Stop condition |
|---|---|---|
| Restore customer state | Located encrypted archive, independent decryption key, SHA256, archive date, successful isolated SQLCipher and Hiddify restore, reconciled provider payment records | Missing key, corrupt/partial database, unknown newest writer or unreconciled payments |
| Fresh staging only | Owner explicitly accepts that no verified customer restore is available; use test accounts and test billing credentials | Existing customer entitlements/payments would be silently recreated, discarded or marked paid |

A missing backup is a blocker to claiming continuity, not permission to invent
customers or replay payments. A new empty system is not a successful restore.
Record acceptable data loss (RPO), recovery time (RTO) and a maintenance deadline
before starting. The retired hosts are not assumed available for rollback.

## Control-plane recovery and payment consistency

1. Inventory every possible writer: bot polling/webhook instances, scheduler,
   payment callbacks, reconciliation workers, database gateways and panel jobs.
   Identify the authoritative database and Hiddify version/image; stop if ambiguous.
2. In an approved maintenance window, fence all writers and record the last
   processed payment/event identifiers. Configure provider retry or a durable
   private callback queue; never acknowledge an event that has not been persisted.
3. Create the final consistent SQLCipher snapshot using the supported backup
   mechanism, accounting for WAL. Export Hiddify by the method supported by the
   pinned version. Verify checksums and integrity on an isolated restore. Do not
   copy a live SQLite file alone or mix incompatible Hiddify releases.
4. Record the new control-plane and database host assignments explicitly. If the
   DB gateway is used, allow only the intended WG/private peer; externally test
   that its port and management APIs are not publicly reachable. Do not copy
   legacy host aliases or firewall rules without validating their new targets.
5. Rotate exposed Telegram, SMTP, payment, panel, gateway and tunnel credentials
   through each issuer. Store replacements in restricted files/mounted secrets.
   Record only credential identifiers and rotation status in private evidence;
   never values. Verify replacement operation, revoke old values and verify they
   fail. Where overlap is supported, coordinate the change so callbacks are not lost.
6. Start exactly one authorized bot and one set of writing workers. Polling and
   webhook modes must not run competing instances. Keep paid activation disabled
   until reconciliation is complete.
7. Reconcile pending/successful/refunded payments against the provider's record,
   using existing event/payment idempotency keys. Test repeated and late callbacks:
   no double charge, duplicated grant, lost expiry or premature activation.
8. Prove per-user provisioning, expiry, quota, usage and revoke on both NL exits.
   The generator's static staging UUIDs must never become paid subscriber accounts.

## Cutover and rollback with new writes

Record the pre-cutover release, private configuration snapshot, authoritative
database identity and write watermark. Prestage DNS/TLS on the new services and
verify them with test clients before switching customer records. A maintenance
window may be necessary; this document does not promise zero downtime.

| Failure point | Required response |
|---|---|
| Before any new write | Stop staged services, restore the verified local pre-change configuration/database on the new infrastructure; resume only the chosen writer after integrity checks |
| After new writes or payment callbacks | Fence writers again. Preserve the newest database/WAL and callback queue. Restore a compatible release against that state or explicitly reconcile/export the delta into a verified restore. Never point the bot at an older database just to recover HTTP health |
| VPN node failure | Withdraw only the failed route; test a permitted route for every affected segment. Do not cross independent REALITY keypairs behind one fixed client profile |
| Subscription filtering failure | Keep strict isolation; use the last verified filtering release or return 502/503 while clients retain their installed profiles. Never fall back to the raw panel URL or disable sharding to obtain a green health check |
| Deadline exceeded / data integrity uncertain | Keep paid activation stopped, preserve evidence, and ask the owner to choose a recovery path; do not overwrite the only current copy |

DNS rollback alone cannot reverse database writes or payment side effects. Test
the post-write recovery procedure on an isolated copy before production cutover.
Only the owner can approve any intentional loss of reconciled customer state.

## Subscription and launch acceptance

- Primary and backup subscription origins must enforce the same authentication,
  segment mapping, strict filtering and rate limits. Verify the raw Hiddify user
  subscription cannot be fetched directly with a customer's UUID.
- Verify the compatibility boundary in [SUBSCRIPTION_SECURITY_BOUNDARY.md](SUBSCRIPTION_SECURITY_BOUNDARY.md).
  Unsupported JSON/transports may produce filtered links or a controlled error;
  test this with each supported client version before mass rollout.
- Document the selected all-routes / isolated-pairs policy. If a segment has only
  one RU→NL pair, explicitly label it as lacking the other pair's failover. Do
  not call an inaccessible backup route available to that segment.
- Test each segment from at least three Russian access networks, including mobile
  and fixed service: actual encrypted traffic, expected egress, subscription refresh,
  failure of one entry/exit, reboot, expiry/usage and revoke. Record date/client
  version and measurable acceptance thresholds before testing.
- Verify website/TLS/login, payment signature handling and reconciliation, one
  active writer, encrypted offsite backup and isolated restore. Keep credentials,
  raw inspect, databases and generated links outside this public repository.
- Complete the [release gate](FOUR_NODE_RELEASE_READINESS.md) with fresh evidence,
  including post-write rollback and raw-subscription isolation. Its READY result
  is self-attested, not an independent inspection or deployment authorization.
