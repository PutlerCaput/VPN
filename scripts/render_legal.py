"""Render the public legal documents (legal/*.md) to readable HTML pages.

legal/*.md stays the single source: the bot and the apps read the same files.
Only the Markdown these documents use is supported: headings, paragraphs,
**bold**, "- " lists and tables. Text is HTML-escaped before any markup is
added, so a document cannot inject tags. The pages carry no scripts. The look
lives in legal_page/style.css, the frame in legal_page/page.html.

    python scripts/render_legal.py --source legal --out build/legal
"""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

ASSETS = Path(__file__).resolve().parent / "legal_page"
DOCUMENTS: tuple[tuple[str, str], ...] = (
    (
        "oferta",
        "Условия услуги «Сервис защищённого доступа»: тарифы, оплата, возврат "
        "и ответственность сторон.",
    ),
    (
        "privacy",
        "Какие персональные данные обрабатывает сервис Cyberdefency, зачем, "
        "сколько хранит и как их удалить.",
    ),
)
DEFAULT_ORIGIN = "https://app.cyberdefency.com"
ORIGIN_RE = re.compile(r"^https://[a-z0-9.-]+(:\d{1,5})?$")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*\.)\s+")
TABLE_RULE_RE = re.compile(r"^\|?\s*:?-{3,}")


def inline(text: str) -> str:
    """Escaped text with **bold** as <strong>."""
    return BOLD_RE.sub(r"<strong>\1</strong>", html.escape(text, quote=False))


def plain(text: str) -> str:
    """Attribute-safe text without Markdown marks."""
    return html.escape(BOLD_RE.sub(r"\1", text), quote=True)


@dataclass
class Page:
    title: str = ""
    meta: str = ""
    toc: list[tuple[str, str]] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)


def _paragraph(text: str) -> str:
    clause = CLAUSE_RE.match(text)
    if clause is None:
        return f"<p>{inline(text)}</p>"
    rest = text[clause.end() :]
    return f'<p><span class="clause">{clause.group(1)}</span> {inline(rest)}</p>'


def _table(rows: list[list[str]]) -> str:
    head, *body = rows
    header = "".join(f'<th scope="col">{inline(cell)}</th>' for cell in head)
    lines = []
    for row in body:
        cells = "".join(
            f'<td data-label="{plain(label)}">{inline(cell)}</td>'
            for label, cell in zip(head, row, strict=False)
        )
        lines.append(f"<tr>{cells}</tr>")
    return (
        f'<div class="table"><table><thead><tr>{header}</tr></thead>'
        f"<tbody>{''.join(lines)}</tbody></table></div>"
    )


def render_markdown(text: str) -> Page:
    page = Page()
    paragraph: list[str] = []
    items: list[str] = []
    rows: list[list[str]] = []

    def flush() -> None:
        if paragraph:
            joined = " ".join(paragraph)
            if not page.toc and not page.meta and joined.startswith("Редакция"):
                page.meta = joined
            else:
                page.blocks.append(_paragraph(joined))
            paragraph.clear()
        if items:
            page.blocks.append(
                "<ul>" + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ul>"
            )
            items.clear()
        if rows:
            page.blocks.append(_table([list(row) for row in rows]))
            rows.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("|"):
            if paragraph or items:
                flush()
            if not TABLE_RULE_RE.match(line):
                rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif line.startswith("# "):
            flush()
            page.title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            heading = line[3:].strip()
            anchor = f"s{len(page.toc) + 1}"
            page.toc.append((anchor, heading))
            page.blocks.append(f'<h2 id="{anchor}">{inline(heading)}</h2>')
        elif line.startswith(("- ", "* ")):
            if paragraph or rows:
                flush()
            items.append(line[2:].strip())
        else:
            if items or rows:
                flush()
            paragraph.append(line)
    flush()
    return page


def render_document(markdown: str, *, slug: str, description: str, origin: str) -> str:
    if not ORIGIN_RE.fullmatch(origin):
        raise ValueError("origin must look like https://host[:port]")
    page = render_markdown(markdown)
    toc = ""
    if page.toc:
        links = "".join(
            f'<li><a href="#{anchor}">{inline(text)}</a></li>'
            for anchor, text in page.toc
        )
        toc = (
            '<nav class="toc" aria-label="Содержание">'
            f'<p class="toc-title">Содержание</p><ul>{links}</ul></nav>'
        )
    frame = Template((ASSETS / "page.html").read_text(encoding="utf-8"))
    return frame.substitute(
        description=html.escape(description, quote=True),
        title_attr=plain(page.title),
        title_text=html.escape(BOLD_RE.sub(r"\1", page.title), quote=False),
        title_html=inline(page.title),
        origin=origin,
        slug=slug,
        style=(ASSETS / "style.css").read_text(encoding="utf-8"),
        meta=f'<p class="meta">{inline(page.meta)}</p>' if page.meta else "",
        toc=toc,
        body="\n".join(page.blocks),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("legal"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for slug, description in DOCUMENTS:
        source = (args.source / f"{slug}.md").read_text(encoding="utf-8")
        page = render_document(
            source, slug=slug, description=description, origin=args.origin
        )
        (args.out / f"{slug}.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
