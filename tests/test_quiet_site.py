"""The site is open to whoever has the link and is not offered to search engines.

The offer and the privacy policy must stay readable (152-FZ), so nothing here
closes a page. The pages only ask crawlers not to list, follow or cache them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.render_legal import DOCUMENTS, render_document

ROOT = Path(__file__).resolve().parents[1]
ENTRY_PAGES = (
    "pwa/index.html",
    "pwa/public/connect.html",
    "tma/index.html",
    "scripts/legal_page/page.html",
)
ROBOTS_META = re.compile(r'<meta name="robots" content="([^"]*)"\s*/?>')


@pytest.mark.parametrize("page", ENTRY_PAGES)
def test_every_page_asks_search_engines_not_to_list_it(page: str) -> None:
    html = (ROOT / page).read_text(encoding="utf-8")
    found = ROBOTS_META.search(html)
    assert found is not None
    directives = {part.strip() for part in found.group(1).split(",")}
    assert {"noindex", "nofollow"} <= directives


@pytest.mark.parametrize("slug", [slug for slug, _ in DOCUMENTS])
def test_the_rendered_legal_pages_keep_the_robots_meta(slug: str) -> None:
    source = (ROOT / "legal" / f"{slug}.md").read_text(encoding="utf-8")
    page = render_document(
        source, slug=slug, description="d", origin="https://app.test"
    )
    found = ROBOTS_META.search(page)
    assert found is not None
    assert "noindex" in found.group(1)


def test_every_answer_of_the_site_carries_the_noindex_header() -> None:
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    # The first header block is the site-wide one; the handlers below it add to
    # the answer and never replace it.
    site_wide = caddy.split("header {", 1)[1].split("\n\t}", 1)[0]
    assert 'X-Robots-Tag "noindex, nofollow, noarchive"' in site_wide
    assert "-X-Robots-Tag" not in caddy


def test_crawling_stays_open_so_that_the_instruction_can_be_read() -> None:
    # A crawler that may not fetch a page never sees its noindex. Only the API
    # and the Mini App, which are no content, are closed to it.
    lines = (ROOT / "pwa/public/robots.txt").read_text(encoding="utf-8").splitlines()
    rules = [line.strip() for line in lines if line.strip() and line[0] != "#"]
    assert "Disallow: /" not in rules
    assert "Disallow: /api/" in rules
    assert not any(rule.lower().startswith("sitemap") for rule in rules)
