"""scripts/check_client_apps.py: every app the menu offers is asked about at its
source, and a vanished or archived app is a PROBLEM."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts import check_client_apps as checker
from services.connection_guide import GUIDES


def days_ago(days: int) -> str:
    moment = datetime.now(UTC) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Source:
    """Stands in for Apple and GitHub: answers by address, records the questions."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.store: dict[str, dict[str, Any]] = {}
        self.repos: dict[str, dict[str, Any]] = {}
        self.fail = False

    def __call__(self, url: str) -> Any:
        self.asked.append(url)
        if self.fail:
            raise OSError("no route")
        if "itunes.apple.com" in url:
            app_id = re.search(r"id=(\d+)", url)
            assert app_id
            found = self.store.get(app_id[1])
            return {
                "resultCount": 1 if found else 0,
                "results": [found] if found else [],
            }
        repo = re.search(r"/repos/([^/]+/[^/]+)(/releases/latest)?$", url)
        assert repo
        data = self.repos[repo[1]]
        return data["release"] if repo[2] else data["repo"]


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> Source:
    fake = Source()
    monkeypatch.setattr(checker, "fetch_json", fake)
    return fake


def healthy(source: Source) -> None:
    """Every offered app exists, is fresh and is not archived."""
    for url in checker.offered_apps():
        if match := checker.STORE_URL.match(url):
            source.store[match.group(1)] = {
                "version": "1.0",
                "currentVersionReleaseDate": days_ago(20),
            }
        elif match := checker.GITHUB_URL.match(url):
            source.repos[f"{match.group(1)}/{match.group(2)}"] = {
                "repo": {"archived": False},
                "release": {"tag_name": "v1", "published_at": days_ago(30)},
            }


def test_every_offered_app_is_known_to_the_checker() -> None:
    urls = checker.offered_apps()
    assert len(urls) >= 6  # the five apps and AmneziaVPN
    for url in urls:
        assert checker.STORE_URL.match(url) or checker.GITHUB_URL.match(url), url
    offered = {a.name for g in GUIDES.values() for a in g.apps}
    assert offered <= {name for name, _ in urls.values()}


def test_a_healthy_list_passes(
    source: Source, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy(source)
    assert checker.main() == 0
    out = capsys.readouterr().out
    assert "CHECK_CLIENT_APPS_OK" in out
    assert "PROBLEM" not in out
    # Only ids and repository names leave: no address of ours, no secret.
    assert all(
        q.startswith(("https://itunes.apple.com/", "https://api.github.com/"))
        for q in source.asked
    )


def test_an_app_missing_from_the_russian_store_is_a_problem(
    source: Source, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy(source)
    del source.store["6472431552"]  # Karing, as V2Box once was
    assert checker.main() == 1
    out = capsys.readouterr().out
    assert re.search(r"^PROBLEM\s+Karing \(ios\): not in the RU App Store", out, re.M)
    assert "CHECK_CLIENT_APPS_PROBLEM" in out
    assert "country=ru" in " ".join(source.asked)


def test_an_archived_repository_is_a_problem(
    source: Source, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy(source)
    source.repos["2dust/v2rayN"]["repo"] = {"archived": True}
    assert checker.main() == 1
    out = capsys.readouterr().out
    assert "PROBLEM" in out
    assert "v2rayN (windows, macos, linux): the repository is archived" in out


def test_an_old_release_is_a_warning_not_a_failure(
    source: Source, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy(source)
    source.repos["MatsuriDayo/NekoBoxForAndroid"]["release"]["published_at"] = days_ago(
        500
    )
    source.store["6745748773"]["currentVersionReleaseDate"] = days_ago(400)
    assert checker.main() == 0
    out = capsys.readouterr().out
    assert re.search(
        r"^WARN\s+NekoBox \(android\): latest release is 500 days old", out, re.M
    )
    assert re.search(
        r"^WARN\s+OneXray \(ios, macos\): no update for 400 days", out, re.M
    )


def test_no_network_is_a_warning_never_a_removal(
    source: Source, capsys: pytest.CaptureFixture[str]
) -> None:
    source.fail = True
    assert checker.main() == 0
    out = capsys.readouterr().out
    assert out.count("WARN") == len(checker.offered_apps())
    assert "could not ask" in out


def test_an_address_the_checker_does_not_know_is_a_problem() -> None:
    assert checker.check("https://example.org/app")[0] == checker.PROBLEM
    assert checker.check("https://play.google.com/store/apps/details?id=x")[0] == (
        checker.PROBLEM
    )


def test_fetch_json_refuses_anything_but_https() -> None:
    with pytest.raises(ValueError, match="https"):
        checker.fetch_json("http://itunes.apple.com/lookup?id=1")
    with pytest.raises(ValueError, match="https"):
        checker.fetch_json("file:///etc/passwd")
