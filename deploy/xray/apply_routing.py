"""Stage an Xray policy candidate with verified registry data; never restart services.

Usage: python3 apply_routing.py CONFIG --assets-dir XRAY_ASSETS_DIRECTORY
Output defaults to CONFIG.candidate.json; the input remains unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DOMAIN_RE = re.compile(r"^(domain|full|keyword):[A-Za-z0-9.\-]{1,253}$")


def load_custom_domains(path: Path) -> list[str]:
    domains = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not DOMAIN_RE.fullmatch(line):
            sys.exit(f"{path.name}:{number}: unsupported entry {line!r}")
        domains.append(line)
    return domains


def merge(
    config: dict[str, Any],
    template: dict[str, Any],
    custom: list[str],
    allowed: list[str] | None = None,
    registry_file: str | None = None,
) -> None:
    rules = [dict(rule) for rule in template["routing"]["rules"]]
    tags = {rule["ruleTag"] for rule in rules} | {"ru-registry-block"}
    for rule in rules:
        if rule["ruleTag"] == "owner-platform-exceptions":
            rule["domain"] = list(allowed if allowed is not None else rule["domain"])
            if registry_file:
                rule["domain"].extend(
                    f"ext:{registry_file}:{category}"
                    for category in (
                        "category-social-media-!cn",
                        "category-social-media-cn",
                        "category-social-media-ir",
                        "category-games",
                    )
                )
        if rule["ruleTag"] == "aup-domains":
            rule["domain"] = [*rule["domain"], *custom]
    rules = [r for r in rules if r.get("domain") != []]
    if registry_file:
        index = next(i for i, r in enumerate(rules) if r["ruleTag"] == "aup-domains")
        rules.insert(
            index,
            {
                "ruleTag": "ru-registry-block",
                "type": "field",
                "outboundTag": "BLOCK",
                "domain": [f"ext:{registry_file}:ru-blocked-all"],
            },
        )
    config.setdefault("log", {}).update(
        {"loglevel": "none", "access": "none", "error": "none"}
    )
    routing = config.setdefault("routing", {})
    routing["domainStrategy"] = template["routing"]["domainStrategy"]
    kept = [r for r in routing.get("rules", []) if r.get("ruleTag") not in tags]
    routing["rules"] = rules + kept

    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") == "vless":
            inbound.setdefault("sniffing", {}).update(
                {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": True,
                    "metadataOnly": False,
                }
            )
    outbounds = config.setdefault("outbounds", [])
    for outbound in outbounds:
        expected = {"DIRECT": "freedom", "BLOCK": "blackhole"}.get(outbound.get("tag"))
        if expected and outbound.get("protocol") != expected:
            raise ValueError("Existing policy outbound has an incompatible protocol")
    present = {o.get("tag") for o in outbounds}
    for outbound in template["outbounds"]:
        if outbound["tag"] not in present:
            # DIRECT must stay first if it is missing: Xray uses the first
            # outbound as the default route.
            if outbound["tag"] == "DIRECT":
                outbounds.insert(0, outbound)
            else:
                outbounds.append(outbound)


def verified_registry(directory: Path) -> tuple[Path, str]:
    metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    filename = metadata["file"]
    if not isinstance(filename, str) or not re.fullmatch(
        r"ru-registry-[a-f0-9]{64}\.dat", filename
    ):
        raise ValueError("Invalid registry asset name")
    source = directory / filename
    with source.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != metadata["sha256"] or filename != f"ru-registry-{actual}.dat":
        raise ValueError("Registry asset SHA256 mismatch")
    categories = metadata["categories"]
    required = {
        "ru-blocked-all",
        "category-games",
        "category-social-media-!cn",
        "category-social-media-cn",
        "category-social-media-ir",
    }
    if not required.issubset(categories):
        raise ValueError("Missing registry categories")
    return source, filename


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--registry-dir", type=Path, default=HERE / "registry")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv[1:])
    target = args.config
    output = args.output or target.with_name(target.name + ".candidate.json")
    if output.resolve() == target.resolve():
        raise ValueError("Output must differ from active config")
    config = json.loads(target.read_text(encoding="utf-8"))
    template = json.loads((HERE / "routing-rules.json").read_text(encoding="utf-8"))
    custom = load_custom_domains(HERE / "custom-blocklist.txt")
    allowed = load_custom_domains(HERE / "platform-allowlist.txt")
    allowed += load_custom_domains(HERE / "news-allowlist.txt")
    if any(not value.startswith(("domain:", "full:")) for value in allowed):
        raise ValueError("Exceptions must be explicit domains, not keywords")
    source, filename = verified_registry(args.registry_dir)
    created_assets = not args.assets_dir.exists()
    args.assets_dir.mkdir(parents=True, exist_ok=True)
    if created_assets:
        args.assets_dir.chmod(0o755)
    asset = args.assets_dir / filename
    if asset.resolve() != source.resolve():
        # New content-addressed filename; never overwrite another registry version.
        if asset.exists():
            with asset.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != filename[12:-4]:
                    raise ValueError("Existing destination asset is corrupt")
        else:
            with asset.open("xb") as stream, source.open("rb") as original:
                os.fchmod(stream.fileno(), 0o644)
                shutil.copyfileobj(original, stream)
                stream.flush()
                os.fsync(stream.fileno())
    merge(config, template, custom, list(dict.fromkeys(allowed)), filename)
    # Exclusive creation protects prior reviewed candidates. A failed write does
    # not affect the active input file; remove only this failed candidate manually.
    with output.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(
        f"Policy candidate written to {output}; "
        "validate with the actual Xray binary before activation."
    )


if __name__ == "__main__":
    main(sys.argv)
