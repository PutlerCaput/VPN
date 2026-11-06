#!/usr/bin/env python3
"""Download verified public GeoSite data; stage it without restarting production."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil

# Fixed read-only Git arguments; no shell or downloaded commands.
import subprocess  # nosec B404
import tempfile
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY = "https://github.com/runetfreedom/russia-blocked-geosite.git"
RAW = "https://raw.githubusercontent.com/runetfreedom/russia-blocked-geosite/"
MAX_BYTES = 96 * 1024 * 1024


def varint(data: memoryview, offset: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated protobuf integer")
        value = data[offset]
        offset += 1
        result |= (value & 127) << shift
        if value < 128:
            return result, offset
    raise ValueError("Oversized protobuf integer")


def fields(data: memoryview) -> Iterator[tuple[int, memoryview | int]]:
    offset = 0
    while offset < len(data):
        key, offset = varint(data, offset)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value, offset = varint(data, offset)
            yield field, value
        elif wire == 2:
            size, offset = varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("Truncated protobuf field")
            yield field, data[offset:end]
            offset = end
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
            if offset > len(data):
                raise ValueError("Truncated fixed-width field")
        else:
            raise ValueError("Unsupported protobuf wire type")


def catalog(data: bytes) -> dict[str, int]:
    result: dict[str, int] = {}
    for field, entry in fields(memoryview(data)):
        if field != 1 or not isinstance(entry, memoryview):
            raise ValueError("Unexpected GeoSite container")
        name = None
        count = 0
        for tag, value in fields(entry):
            if tag == 1 and isinstance(value, memoryview):
                name = bytes(value).decode("ascii").lower()
            elif tag == 2 and isinstance(value, memoryview):
                count += 1
        if not name or not re.fullmatch(r"[a-z0-9@!_.-]+", name) or name in result:
            raise ValueError("Invalid or duplicate GeoSite category")
        result[name] = count
    if result.get("ru-blocked-all", 0) < 1000:
        raise ValueError("Missing or unexpectedly small blocked-domain category")
    return result


def encode_varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


KEPT_CATEGORIES = frozenset(
    {
        "ru-blocked-all",
        "category-social-media-!cn",
        "category-social-media-cn",
        "category-social-media-ir",
        "category-games",
    }
)


def select_categories(data: bytes) -> bytes:
    output = bytearray()
    for field, entry in fields(memoryview(data)):
        if field != 1 or not isinstance(entry, memoryview):
            raise ValueError("Unexpected GeoSite container")
        name = next(
            (
                bytes(v).decode("ascii").lower()
                for k, v in fields(entry)
                if k == 1 and isinstance(v, memoryview)
            ),
            None,
        )
        if name in KEPT_CATEGORIES:
            output += b"\x0a" + encode_varint(len(entry))
            output += entry
    selected = catalog(bytes(output))
    if not KEPT_CATEGORIES.issubset(selected):
        raise ValueError("Source is missing required platform categories")
    return bytes(output)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ValueError("Registry download redirects are forbidden")


def download(url: str, limit: int) -> bytes:
    if not url.startswith(RAW):
        raise ValueError("Unsupported registry source")
    request = urllib.request.Request(  # noqa: S310  # nosec B310
        url, headers={"User-Agent": "NodeRental-Registry/1.0"}
    )
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=60) as response:
        if response.status != 200 or not response.geturl().startswith(RAW):
            raise ValueError("Unexpected registry response or redirect")
        payload: bytes = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Registry exceeds size limit")
    return payload


def atomic_write(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage(directory: Path, commit: str | None = None) -> dict[str, object]:
    if commit is None:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("git is required to resolve a release commit")
        output = subprocess.run(  # noqa: S603  # nosec B603
            [str(Path(git).resolve()), "ls-remote", REPOSITORY, "refs/heads/release"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.split()
        if len(output) != 2 or output[1] != "refs/heads/release":
            raise ValueError("Cannot resolve release commit")
        commit = output[0]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected full immutable Git commit SHA")
    url = RAW + commit + "/geosite.dat"
    expected = download(url + ".sha256sum", 4096).decode("ascii").split()[0]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError("Invalid source SHA256")
    payload = download(url, MAX_BYTES)
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(expected.lower(), actual):
        raise ValueError("GeoSite SHA256 mismatch")
    source_sha256 = actual
    payload = select_categories(payload)
    categories = catalog(payload)
    actual = hashlib.sha256(payload).hexdigest()
    filename = f"ru-registry-{actual}.dat"
    created_directory = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if created_directory:
        directory.chmod(0o755)
    atomic_write(directory / filename, payload)
    if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != actual:
        raise ValueError("Stored GeoSite SHA256 mismatch")
    metadata: dict[str, object] = {
        "source_repository": REPOSITORY,
        "source_url": url,
        "source_commit": commit,
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "file": filename,
        "sha256": actual,
        "blocked_category": "ru-blocked-all",
        "blocked_entries": categories["ru-blocked-all"],
        "categories": categories,
        "source_sha256": source_sha256,
        "source_kind": "community aggregation; not an official RKN export",
    }
    atomic_write(
        directory / "manifest.json",
        (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode(),
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).parent / "registry"
    )
    parser.add_argument(
        "--commit", help="Optional immutable release commit to reproduce"
    )
    args = parser.parse_args()
    metadata = stage(args.output_dir, args.commit)
    print(
        json.dumps(
            {
                key: metadata[key]
                for key in (
                    "source_commit",
                    "file",
                    "sha256",
                    "blocked_entries",
                    "downloaded_at_utc",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
