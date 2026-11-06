from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/xray/update_registry.py"
spec = importlib.util.spec_from_file_location("update_registry_test", SCRIPT)
assert spec is not None
assert spec.loader is not None
registry: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def fixture_data() -> bytes:
    def field(number: int, data: bytes) -> bytes:
        return (
            registry.encode_varint((number << 3) | 2)
            + registry.encode_varint(len(data))
            + data
        )

    domain = b"\x08\x03" + field(2, b"blocked.invalid")
    result = b""
    for category in sorted(registry.KEPT_CATEGORIES):
        count = 1000 if category == "ru-blocked-all" else 1
        site = field(1, category.upper().encode()) + field(2, domain) * count
        result += field(1, site)
    return result


def test_checked_download_and_compiled_snapshot(
    tmp_path: Path, monkeypatch: Any
) -> None:
    data = fixture_data()
    checksum = hashlib.sha256(data).hexdigest().encode() + b"  geosite.dat\n"
    monkeypatch.setattr(
        registry,
        "download",
        lambda url, limit: checksum if url.endswith("sha256sum") else data,
    )
    metadata = registry.stage(tmp_path, "a" * 40)
    assert metadata["blocked_entries"] == 1000
    stored = (tmp_path / metadata["file"]).read_bytes()
    assert hashlib.sha256(stored).hexdigest() == metadata["sha256"]
    assert (
        json.loads((tmp_path / "manifest.json").read_text())["source_commit"]
        == "a" * 40
    )


def test_checksum_failure_preserves_last_snapshot(
    tmp_path: Path, monkeypatch: Any
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"previous-verified-snapshot")
    monkeypatch.setattr(
        registry,
        "download",
        lambda url, limit: b"0" * 64 if url.endswith("sha256sum") else b"corrupt",
    )
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        registry.stage(tmp_path, "a" * 40)
    assert manifest.read_bytes() == b"previous-verified-snapshot"
    assert list(tmp_path.glob("*.dat")) == []


def test_corrupt_data_and_untrusted_commit_are_rejected(tmp_path: Path) -> None:
    for data in (b"\x0a\x80", b"\x0a\xff\xff\xff\xff\x7f", b"\x00", b""):
        with pytest.raises(ValueError, match="Truncated|Invalid|Missing|Unexpected"):
            registry.catalog(data)
    with pytest.raises(ValueError, match="immutable"):
        registry.stage(tmp_path, "../../untrusted")
