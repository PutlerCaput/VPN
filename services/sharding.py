"""Configuration sharding: each user sees only a subset of the entry nodes.

A *segment* is a named group of entry hosts (RU relays, direct addresses). A
user is put into one segment: deterministically from their Telegram id (HMAC,
so the mapping cannot be recomputed without the server key), pinned in the
``entry_segments`` table, and changeable by the admin
(``scripts/entry_segments.py``). The subscription the user downloads is the
Hiddify one, filtered here: an entry whose host belongs to another segment is
removed, so one parsed subscription never reveals the whole pool.

Everything here is pure (no I/O); the HTTP side is ``api/routes.py`` (``/sub``)
and the fetch is ``services.hiddify.HiddifyClient.fetch_subscription``.
Off by default: ``ENTRY_SHARDING_ENABLED=false`` leaves issuance unchanged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

# Link schemes whose host is the authority part of the URI.
URI_SCHEMES = ("vless", "trojan", "hysteria2", "hy2", "hysteria", "tuic", "ss")
# Outbound types in sing-box / Xray JSON that only group other outbounds.
GROUP_TYPES = frozenset({"selector", "urltest"})
FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "edge", "random", "randomized")


def normalize_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


@dataclass(frozen=True)
class SegmentPlan:
    """Entry segments and hosts every segment also gets."""

    segments: Mapping[str, frozenset[str]]
    shared: frozenset[str] = frozenset()
    strict: bool = True

    @classmethod
    def build(
        cls,
        segments: Mapping[str, Iterable[str]],
        shared: Iterable[str] = (),
        *,
        strict: bool = True,
    ) -> SegmentPlan:
        return cls(
            segments={
                name: frozenset(normalize_host(h) for h in hosts if h.strip())
                for name, hosts in segments.items()
            },
            shared=frozenset(normalize_host(h) for h in shared if h.strip()),
            strict=strict,
        )

    @property
    def names(self) -> list[str]:
        return sorted(self.segments)

    @property
    def pool(self) -> frozenset[str]:
        hosts: set[str] = set(self.shared)
        for members in self.segments.values():
            hosts |= members
        return frozenset(hosts)

    def allowed(self, segment: str) -> frozenset[str]:
        return self.segments.get(segment, frozenset()) | self.shared

    def keeps(self, segment: str, host: str | None) -> bool:
        """True when an entry on ``host`` may be shown to a user of ``segment``."""
        if not host:
            return not self.strict
        host = normalize_host(host)
        if host in self.allowed(segment):
            return True
        # A host of another segment is never shown; an unknown one only when
        # strict mode is off (strict: only listed hosts reach anyone).
        return not self.strict and host not in self.pool


def assign_segment(telegram_id: int, names: list[str], key: bytes) -> str:
    """Deterministic segment for a user; stable while the segment list is."""
    if not names:
        raise ValueError("no entry segments configured")
    ordered = sorted(names)
    digest = hmac.new(key, f"entry-segment:{telegram_id}".encode(), hashlib.sha256)
    return ordered[int.from_bytes(digest.digest()[:8], "big") % len(ordered)]


def derive_key(secret: str) -> bytes:
    """Sharding key from a server secret, separated from its other uses."""
    return hmac.new(
        secret.encode(), b"cyberdefency/entry-sharding/v1", hashlib.sha256
    ).digest()


# ------------------------------------------------------------------ links
def link_host(line: str) -> str | None:
    """Host of a proxy link, or None when it cannot be read."""
    scheme, _, rest = line.partition("://")
    scheme = scheme.lower()
    if scheme == "vmess":
        try:
            padded = rest.split("#", 1)[0] + "=" * (-len(rest) % 4)
            data = json.loads(base64.b64decode(padded, validate=False))
        except (ValueError, binascii.Error):
            return None
        address = data.get("add") if isinstance(data, dict) else None
        return normalize_host(str(address)) if address else None
    if scheme not in URI_SCHEMES:
        return None
    try:
        host = urlsplit(line).hostname
    except ValueError:
        return None
    return normalize_host(host) if host else None


def with_link_fingerprint(line: str, fingerprint: str) -> str:
    """Sets ``fp`` on a TLS/REALITY vless or trojan link; other links unchanged."""
    scheme = line.partition("://")[0].lower()
    if scheme not in ("vless", "trojan"):
        return line
    try:
        parts = urlsplit(line)
    except ValueError:
        return line
    query = parse_qsl(parts.query, keep_blank_values=True)
    security = dict(query).get("security", "")
    if security not in ("reality", "tls"):
        return line
    query = [(k, v) for k, v in query if k != "fp"] + [("fp", fingerprint)]
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query, quote_via=quote),
            parts.fragment,
        )
    )


def _decode_base64(body: str) -> str | None:
    compact = "".join(body.split())
    if not compact or "://" in compact:
        return None
    try:
        text = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
        decoded = text.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        try:
            decoded = base64.urlsafe_b64decode(
                compact + "=" * (-len(compact) % 4)
            ).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
    return decoded if "://" in decoded else None


@dataclass
class Filtered:
    body: str
    kept: int
    removed: int
    kind: str  # links | base64 | singbox | xray


def _filter_lines(
    text: str, plan: SegmentPlan, segment: str, fp: str | None
) -> tuple[list[str], int]:
    kept: list[str] = []
    removed = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            removed += 1
            continue
        if not plan.keeps(segment, link_host(line)):
            removed += 1
            continue
        kept.append(with_link_fingerprint(line, fp) if fp else line)
    return kept, removed


def _outbound_host(outbound: Mapping[str, Any]) -> str | None:
    if isinstance(outbound.get("server"), str):  # sing-box
        return normalize_host(outbound["server"])
    settings = outbound.get("settings")
    if isinstance(settings, Mapping):  # Xray
        for key in ("vnext", "servers"):
            items = settings.get(key)
            if isinstance(items, list) and items and isinstance(items[0], Mapping):
                address = items[0].get("address")
                if isinstance(address, str):
                    return normalize_host(address)
    return None


def _is_proxy(outbound: Mapping[str, Any]) -> bool:
    kind = str(outbound.get("type") or outbound.get("protocol") or "")
    return kind not in GROUP_TYPES and _outbound_host(outbound) is not None


def _filter_xray_servers(
    outbound: dict[str, Any], plan: SegmentPlan, segment: str
) -> tuple[bool, int] | None:
    """Filter every Xray endpoint, not just the first one in an outbound.

    An Xray vnext/servers outbound can contain addresses from several segments.
    Checking only its first address leaks every remaining address to the user.
    Unknown or malformed entries are dropped in strict mode (fail closed).
    """
    settings = outbound.get("settings")
    if not isinstance(settings, dict):
        return None
    keys = [key for key in ("vnext", "servers") if key in settings]
    if not keys:
        return None
    kept = removed = 0
    for key in keys:
        endpoints = settings[key]
        # A malformed address-bearing field must never be sent unchanged.
        if not isinstance(endpoints, list):
            return False, removed + kept + 1
        allowed = [
            entry
            for entry in endpoints
            if plan.keeps(
                segment,
                entry.get("address")
                if isinstance(entry, dict) and isinstance(entry.get("address"), str)
                else None,
            )
        ]
        settings[key] = allowed
        kept += len(allowed)
        removed += len(endpoints) - len(allowed)
    return bool(kept), removed


def _set_outbound_fingerprint(outbound: dict[str, Any], fp: str) -> None:
    tls = outbound.get("tls")
    if isinstance(tls, dict) and tls.get("enabled"):  # sing-box
        utls = tls.setdefault("utls", {})
        if isinstance(utls, dict):
            utls["enabled"] = True
            utls["fingerprint"] = fp
    stream = outbound.get("streamSettings")
    if isinstance(stream, dict):  # Xray
        for key in ("realitySettings", "tlsSettings"):
            block = stream.get(key)
            if isinstance(block, dict):
                block["fingerprint"] = fp


def _filter_config(
    config: dict[str, Any], plan: SegmentPlan, segment: str, fp: str | None
) -> tuple[int, int]:
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list):
        return 0, 0
    removed_tags: set[str] = set()
    result: list[Any] = []
    kept = removed = 0
    for outbound in outbounds:
        if isinstance(outbound, dict):
            xray = _filter_xray_servers(outbound, plan, segment)
            if xray is not None:
                has_endpoints, dropped = xray
                removed += dropped
                if not has_endpoints:
                    if isinstance(outbound.get("tag"), str):
                        removed_tags.add(outbound["tag"])
                    continue
                kept += 1
                if fp:
                    _set_outbound_fingerprint(outbound, fp)
                result.append(outbound)
                continue
            if _is_proxy(outbound):
                if not plan.keeps(segment, _outbound_host(outbound)):
                    removed += 1
                    if isinstance(outbound.get("tag"), str):
                        removed_tags.add(outbound["tag"])
                    continue
                kept += 1
                if fp:
                    _set_outbound_fingerprint(outbound, fp)
            elif plan.strict and str(
                outbound.get("protocol") or outbound.get("type") or ""
            ).lower() in {
                "vless",
                "vmess",
                "trojan",
                "shadowsocks",
                "socks",
                "http",
                "wireguard",
                "hysteria",
                "hysteria2",
                "tuic",
                "anytls",
            }:
                # Strict subscriptions must not return a malformed proxy whose
                # address could not be parsed. Preserve harmless direct outbounds.
                removed += 1
                if isinstance(outbound.get("tag"), str):
                    removed_tags.add(outbound["tag"])
                continue
        result.append(outbound)
    for outbound in result:
        members = outbound.get("outbounds") if isinstance(outbound, dict) else None
        if isinstance(members, list):
            outbound["outbounds"] = [m for m in members if m not in removed_tags]
            if outbound.get("default") in removed_tags:
                outbound.pop("default", None)
    # A group left empty would make the client reject the whole profile.
    empty = {
        o.get("tag")
        for o in result
        if isinstance(o, dict)
        and str(o.get("type") or "") in GROUP_TYPES
        and not o.get("outbounds")
    }
    result = [
        o
        for o in result
        if not (isinstance(o, dict) and o.get("tag") in empty and o.get("tag"))
    ]
    for outbound in result:
        members = outbound.get("outbounds") if isinstance(outbound, dict) else None
        if isinstance(members, list):
            outbound["outbounds"] = [m for m in members if m not in empty]
    config["outbounds"] = result
    return kept, removed


def filter_subscription(
    body: str, plan: SegmentPlan, segment: str, *, fingerprint: str | None = None
) -> Filtered | None:
    """The subscription with only ``segment``'s entries, or None if unreadable.

    Readable formats: plain link lists, base64 link lists, sing-box JSON and
    Xray JSON (one config or a list of configs). Anything else (Clash YAML,
    HTML) returns None: the caller asks the panel for a link list instead and
    never forwards a format it cannot filter.
    """
    text = body.strip()
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        configs = data if isinstance(data, list) else [data]
        if not configs or not all(
            isinstance(c, dict) and "outbounds" in c for c in configs
        ):
            return None
        kept = removed = 0
        survivors = []
        for config in configs:
            k, r = _filter_config(config, plan, segment, fingerprint)
            kept += k
            removed += r
            if k or not r:
                survivors.append(config)
        result: Any = (
            survivors if isinstance(data, list) else survivors[0] if survivors else {}
        )
        kind = "xray" if isinstance(data, list) else "singbox"
        return Filtered(json.dumps(result, ensure_ascii=False), kept, removed, kind)
    if "://" in text:
        lines, removed = _filter_lines(text, plan, segment, fingerprint)
        return Filtered(
            "\n".join(lines) + ("\n" if lines else ""), len(lines), removed, "links"
        )
    decoded = _decode_base64(text)
    if decoded is None:
        return None
    lines, removed = _filter_lines(decoded, plan, segment, fingerprint)
    encoded = base64.b64encode("\n".join(lines).encode()).decode() if lines else ""
    return Filtered(encoded, len(lines), removed, "base64")


# ------------------------------------------------------------- anti-parsing
class FanoutGuard:
    """Caps how many distinct sources may use one subscription per window.

    A subscription shared in a chat or fed to a scraper is fetched from many
    addresses; a person's own devices use a few. Sources are kept as salted
    hashes in memory only, never written anywhere (zero-logs).
    """

    def __init__(
        self, limit: int, window_seconds: float, *, max_keys: int = 50_000
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._seen: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._salt = os.urandom(32)

    def _digest(self, source: str) -> str:
        return hashlib.blake2s(
            source.encode(), key=self._salt[:32], digest_size=8
        ).hexdigest()

    def allow(self, key: str, source: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        sources = self._seen.get(key)
        if sources is None:
            sources = {}
            self._seen[key] = sources
            while len(self._seen) > self.max_keys:
                self._seen.popitem(last=False)
        else:
            self._seen.move_to_end(key)
        for digest, seen in list(sources.items()):
            if seen <= current - self.window:
                del sources[digest]
        digest = self._digest(source)
        if digest in sources:
            sources[digest] = current
            return True
        if len(sources) >= self.limit:
            return False
        sources[digest] = current
        return True


def plan_from_settings(settings: Any) -> SegmentPlan:
    return SegmentPlan.build(
        settings.entry_segments,
        settings.entry_shared_hosts,
        strict=settings.entry_sharding_strict,
    )


async def resolve_segment(
    db: Any, plan: SegmentPlan, telegram_id: int, key: bytes
) -> str:
    """The user's segment: the pinned one if it still exists, else a new pin.

    An admin pin to a segment that was removed from the settings is kept in
    the table (the admin decides) but not used until it exists again.
    """
    row = await db.get_entry_segment(telegram_id)
    if row and row["segment"] in plan.segments:
        return str(row["segment"])
    segment = assign_segment(telegram_id, plan.names, key)
    if not row or row["source"] == "auto":
        await db.set_entry_segment(telegram_id, segment, "auto")
    return segment


def sharded_subscription_url(base_url: str, node_uuid: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/sub/{node_uuid}/"
