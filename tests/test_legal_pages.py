"""Public legal pages: built from legal/*.md, readable, free of injected markup."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.render_legal import DOCUMENTS, render_document, render_markdown

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("slug", [slug for slug, _ in DOCUMENTS])
def test_every_section_list_and_table_is_rendered(slug: str) -> None:
    source = (ROOT / "legal" / f"{slug}.md").read_text(encoding="utf-8")
    lines = source.splitlines()
    page = render_markdown(source)
    body = "\n".join(page.blocks)

    assert page.title == next(
        line[2:].strip() for line in lines if line.startswith("# ")
    )
    sections = sum(1 for line in lines if line.startswith("## "))
    assert body.count("<h2 ") == sections == len(page.toc)
    assert body.count("<li>") == sum(1 for line in lines if line.startswith("- "))
    tables = len(re.findall(r"(?:^\|.*(?:\n|$))+", source, re.M))
    assert body.count("<table>") == tables
    assert "**" not in body
    assert "<p>|" not in body


def test_text_cannot_inject_markup() -> None:
    page = render_markdown("# T\n\nОпасно <script>alert(1)</script> и **важно**\n")
    body = "\n".join(page.blocks)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "<strong>важно</strong>" in body


def test_clause_numbers_are_marked_and_the_revision_goes_to_the_header() -> None:
    page = render_markdown(
        "# T\n\nРедакция от 07.11.2026\n\n## 1. Термины\n\n1.1. **Услуга** — текст.\n"
    )
    assert page.meta == "Редакция от 07.11.2026"
    assert (
        '<span class="clause">1.1.</span> <strong>Услуга</strong> — текст.'
        in page.blocks[1]
    )


def test_table_cells_carry_their_column_names_for_phones() -> None:
    page = render_markdown(
        "| Цель | Данные |\n|---|---|\n| Оплата | **Telegram ID** |\n"
    )
    assert '<td data-label="Цель">Оплата</td>' in page.blocks[0]
    assert '<td data-label="Данные"><strong>Telegram ID</strong></td>' in page.blocks[0]


def test_full_page_has_brand_assets_links_and_no_scripts() -> None:
    source = (ROOT / "legal" / "oferta.md").read_text(encoding="utf-8")
    page = render_document(
        source, slug="oferta", description="d", origin="https://app.test"
    )
    assert "<script" not in page
    assert (
        '<meta property="og:image" content="https://app.test/brand/og-image.jpg">'
        in page
    )
    assert 'href="/oferta.md" download' in page
    assert "/brand/emblem-128.webp" in page
    assert "/fonts/manrope-cyrillic.woff2" in page


@pytest.mark.parametrize(
    "origin", ["http://app.test", "https://app.test/x", 'https://a"b']
)
def test_origin_must_be_a_plain_https_origin(origin: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        render_document("# T\n", slug="oferta", description="d", origin=origin)


def test_caddy_and_the_image_serve_pages_and_downloads() -> None:
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for slug, name in (("oferta", "offer"), ("privacy", "privacy")):
        assert f"rewrite * /{slug}.html" in caddy
        assert f"@public_{name}_md path /{slug}.md" in caddy
        assert (
            f"COPY --from=legal-builder /out/{slug}.html /srv/legal/{slug}.html"
            in docker
        )


def test_the_app_shell_leaves_the_offer_pages_to_the_server() -> None:
    # The PWA service worker answers navigations with index.html unless the
    # path is on this list; the offer and the policy are server pages.
    vite = (ROOT / "pwa" / "vite.config.ts").read_text(encoding="utf-8")
    denylist = vite.split("navigateFallbackDenylist", 1)[1].split("]", 1)[0]
    assert r"/^\/oferta/" in denylist
    assert r"/^\/privacy/" in denylist
