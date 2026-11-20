# CyberDefency: migration checklist for four new VPS (2026-11-20)

> **Status: planning only.** This document does not confirm connectivity, provisioning, deployed services, or working VPN connections. Do not use the old DE/RU runbook for these VPS.

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
