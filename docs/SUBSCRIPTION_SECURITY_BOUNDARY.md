# Subscription filtering and offline security checks

This describes code guarantees and limits. It does not certify a deployed host.

## Supported subscription formats

The API never forwards a subscription schema that it cannot account for.

| Input | Behavior |
|---|---|
| Known plain/base64 URI links | Filter host by segment; accept only known query fields/transports; remove display fragments/VMess labels |
| Basic Xray TCP/raw with TLS/REALITY | Filter every vnext/servers address; validate the remaining structure; replace remarks with the segment label |
| Basic sing-box proxy/direct/group JSON | Validate supported fields, filter server, prune nested groups/defaults |
| Mixed Xray/sing-box object, unknown protocol or extension | Reject the JSON response and retry the panel's link-list format |
| JSON DNS, route/routing, endpoints, XHTTP downloadSettings/extra, other unsupported transport fields | Same filtered link fallback; opaque content is never copied into the output |
| Unreadable fallback / no allowed entries | 502 / 503; no raw panel link and no unfiltered response |

This is intentionally a limited JSON subset, not a general Xray/sing-box
configuration editor. XHTTP links and opaque query extensions are also rejected,
so the fallback cannot reintroduce downloadSettings. Users with only unsupported
profiles may receive an error until a tested supported route exists. Verify
Hiddify/v2rayNG/NekoBox behavior on the exact supported client versions before
rollout; do not imply that a server-side test proves client profile retention.

Known addresses of other segments are also rejected in surviving supported text
fields such as SNI/tags. Public TLS cover names are not themselves entry routes.
Unknown extensions need a schema, explicit destination handling and tests before
being added. Numeric usage/update headers are validated; free-form panel titles,
support URLs and filenames are not forwarded. Sharding does not prevent someone
from discovering addresses from unrelated public sources or raw panel endpoints.

## Secret and private artifact scan

`scripts/secret_scan.sh` delegates to the stdlib Python scanner. With no arguments,
it checks tracked files and non-ignored untracked source files using NUL-delimited
Git enumeration. Explicit filenames use the same rules. Both CI and pre-commit
check binary artifact names too; `.gitignore` is not a security gate.

Checks cover private database/archive/key/env paths, recognizable renamed Docker
inspect/SQLite/archive content, credential assignments in common text formats,
CLI token arguments and private-key/token signatures. Matches print only quoted
file paths, line numbers and finding codes. Read/enumeration errors fail closed.
Symlinks are rejected without reading the target. Known media assets are treated
as binary, not credential-bearing configuration.

Exit codes: 0 = no detected findings; 1 = findings; 2 = incomplete/invalid scan.
This is heuristic detection, not proof that arbitrary obfuscation, binary media
or repository history contain no secrets. Keep the existing gitleaks hook too.
If a credential entered public history, removing it from HEAD is not remediation:
revoke/rotate it and assess exposure separately.

`scripts/secret_scan_allowlist.json` contains SHA256 hashes of exact reviewed
synthetic test lines. It never excludes an entire test file or directory, cannot
exempt a production path, and any byte change invalidates the exemption. Review
every new hash against its source fixture; do not generate broad baselines to
silence findings. Empty/template references and explicit test placeholders are
allowed. Tests verify that ordinary literals in a test directory are still caught.

## Docker inspect preflight

`scripts/resilience_preflight.py` only reads supplied JSON/port text and writes a
redacted report to stdout/stderr. It never calls Docker, SSH, a network endpoint,
or an apply command. The original inspect snapshot itself remains sensitive.

- It checks credential-bearing Env/Cmd/Entrypoint in every supplied container,
  including stopped containers and snapshots without a recognizable bot.
- Bot identity uses the Compose service label (default `bot`) or `--bot-name`;
  multiple candidates are an explicit ambiguity. A legacy name is only a fallback.
- Ports are parsed as local IPv4/IPv6 endpoints. A non-loopback bind is a risk
  indicator, not proof that firewall/Docker publishing makes it internet reachable.
- `--ports` absent is reported as not checked. AWG port comes from agent Env or
  `--awg-port`; process/listener presence never certifies a functioning tunnel.
- Invalid nested JSON/port data returns 2 with only the exception class. Normal
  report mode returns 0 even with HIGH findings; `--fail-high` returns 1 on HIGH.

The tool does not inspect mounted secret contents, test issuer revocation,
validate backups, discover every possible secret encoding, test client traffic,
or prove independent failure domains. See the current four-node runbook for
deployment and live evidence requirements.
