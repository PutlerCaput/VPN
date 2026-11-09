"""Check that every client app the menu offers can still be installed.

  python3 scripts/check_client_apps.py

Reads the apps from services/connection_guide.py and asks the sources
themselves:
  * an App Store app: Apple's public lookup for the Russian storefront. An app
    that Apple took out of that storefront is simply not found there;
  * a GitHub project: its repository record (archived or not) and its latest
    release.
Prints OK, WARN or PROBLEM for each app and exits 1 if anything is a PROBLEM. A
network error is a WARN: the check could not be made. Only an app id or a
repository name is sent. Why these checks: docs/CONNECT_APPS.md.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.connection_guide import AMNEZIA_GUIDE, GUIDES

STOREFRONT = "ru"
STALE_DAYS = 365
TIMEOUT = 20
STORE_URL = re.compile(r"^https://apps\.apple\.com/app/id(\d+)$")
GITHUB_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/releases$")

OK, WARN, PROBLEM = "OK", "WARN", "PROBLEM"


def fetch_json(url: str) -> Any:
    """GET a JSON document over HTTPS. Raises OSError or ValueError on failure."""
    if not url.startswith("https://"):
        raise ValueError(f"not an https address: {url}")
    request = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": "client-app-check", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310  # nosec B310
        return json.load(response)


def age_days(stamp: str) -> int:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (datetime.now(UTC) - moment).days


def check_store(app_id: str) -> tuple[str, str]:
    url = (
        f"https://itunes.apple.com/lookup?id={app_id}"
        f"&country={STOREFRONT}&entity=software"
    )
    try:
        data = fetch_json(url)
    except (OSError, ValueError) as exc:
        return WARN, f"could not ask Apple ({type(exc).__name__})"
    if not data.get("resultCount"):
        return PROBLEM, f"not in the {STOREFRONT.upper()} App Store"
    info = data["results"][0]
    released = str(info.get("currentVersionReleaseDate", ""))
    if released and age_days(released) > STALE_DAYS:
        return WARN, f"no update for {age_days(released)} days (v{info.get('version')})"
    return OK, f"in the {STOREFRONT.upper()} App Store, v{info.get('version')}"


def check_github(owner: str, repo: str) -> tuple[str, str]:
    base = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        record = fetch_json(base)
        if record.get("archived"):
            return PROBLEM, "the repository is archived: no more updates"
        release = fetch_json(f"{base}/releases/latest")
    except (OSError, ValueError) as exc:
        return WARN, f"could not ask GitHub ({type(exc).__name__})"
    published = str(release.get("published_at", ""))
    if not published:
        return WARN, "no release found"
    days = age_days(published)
    if days > STALE_DAYS:
        return WARN, f"latest release is {days} days old ({release.get('tag_name')})"
    return OK, f"latest release {release.get('tag_name')}, {days} days ago"


def offered_apps() -> dict[str, tuple[str, list[str]]]:
    """{download page: (app name, devices)} for every app the bot offers."""
    found: dict[str, tuple[str, list[str]]] = {}
    for device, guide in GUIDES.items():
        for app in guide.apps:
            _, devices = found.setdefault(app.download_url, (app.name, []))
            devices.append(device)
    found.setdefault(AMNEZIA_GUIDE.download_url, (AMNEZIA_GUIDE.client, ["amnezia"]))
    return found


def check(url: str) -> tuple[str, str]:
    if match := STORE_URL.match(url):
        return check_store(match.group(1))
    if match := GITHUB_URL.match(url):
        return check_github(match.group(1), match.group(2))
    return PROBLEM, "an address this check does not know"


def main() -> int:
    worst = OK
    for url, (name, devices) in offered_apps().items():
        status, text = check(url)
        print(f"{status:<7} {name} ({', '.join(devices)}): {text}")
        if status == PROBLEM:
            worst = PROBLEM
    print(
        "CHECK_CLIENT_APPS_PROBLEM: take the apps marked PROBLEM out "
        "(docs/CONNECT_APPS.md)"
        if worst == PROBLEM
        else "CHECK_CLIENT_APPS_OK"
    )
    return 1 if worst == PROBLEM else 0


if __name__ == "__main__":
    sys.exit(main())
