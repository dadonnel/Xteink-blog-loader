"""CrossPoint X4 EPUB creation logic for article bundles."""

from __future__ import annotations

import datetime
import html
import importlib.util
import io
import re
import textwrap
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub

from .config import (
    ARTICLE_QR_CODES,
    BOOK_AUTHOR,
    BOOK_LANGUAGE,
    BOOK_PREFIX,
    LONG_ARTICLE_WORDS,
    IMAGE_MAX_HEIGHT,
    IMAGE_MAX_WIDTH,
    MAX_ARTICLES,
    MAX_IMAGES_PER_ARTICLE,
    MAX_TOTAL_IMAGES,
    MAX_TOTAL_WORDS,
    OUTPUT_DIR,
    QR_SIZE,
    TOC_TITLE_MAX_CHARS,
)

ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "blockquote",
    "ul", "ol", "li", "strong", "b", "em", "i", "br", "hr", "img",
}

CROSSPOINT_CSS = """
h1 { text-align: center; font-weight: bold; margin: 1em 0 0.75em 0; }
h2, h3 { text-align: left; font-weight: bold; margin: 1.25em 0 0.4em 0; }
p { margin: 0 0 0.65em 0; }
.article-meta { font-style: italic; margin: 0 0 1em 0; }
blockquote { font-style: italic; margin: 0.6em 0.5em 0.6em 1em; }
img { width: 100%; margin: 0.7em 0; }
hr { margin: 1em 25%; }
"""


def _trim_title(title: str, limit: int = TOC_TITLE_MAX_CHARS) -> str:
    """Trim a chapter-menu label at a word boundary."""
    title = " ".join(title.split())
    if len(title) <= limit:
        return title
    shortened = title[: limit + 1].rsplit(" ", 1)[0]
    return (shortened or title[:limit]).rstrip(" .,:;-") + "…"


class EpubService:
    def __init__(self, output_dir: Path = OUTPUT_DIR, article_qr_codes: bool = ARTICLE_QR_CODES):
        self.output_dir = Path(output_dir)
        self.article_qr_codes = article_qr_codes

    @staticmethod
    def _sanitize(content: str) -> BeautifulSoup:
        soup = BeautifulSoup(content, "html.parser")
        for tag in list(soup.find_all(["script", "style", "svg", "audio", "video", "form", "iframe", "button"])):
            tag.decompose()

        # Tables and code have no useful native presentation on CrossPoint.
        for table in list(soup.find_all("table")):
            replacement = soup.new_tag("div")
            for row in table.find_all("tr"):
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                if not cells:
                    continue
                line = soup.new_tag("p")
                if len(cells) == 2:
                    label = soup.new_tag("strong")
                    label.string = cells[0] + ": "
                    line.append(label)
                    line.append(cells[1])
                else:
                    line.string = " · ".join(cells)
                replacement.append(line)
            table.replace_with(replacement)
        for code in list(soup.find_all("code")):
            if code.parent and code.parent.name == "pre":
                continue
            strong = soup.new_tag("strong")
            strong.string = code.get_text(" ", strip=True)
            code.replace_with(strong)
        for pre in list(soup.find_all("pre")):
            block = soup.new_tag("div")
            lines = []
            for source_line in pre.get_text("\n").splitlines():
                lines.extend(textwrap.wrap(source_line, width=52, replace_whitespace=False) or [""])
            for index, line_text in enumerate(lines):
                block.append(NavigableString(line_text))
                if index < len(lines) - 1:
                    block.append(soup.new_tag("br"))
            pre.replace_with(block)

        image_count = 0
        for tag in list(soup.find_all(True)):
            if tag.name not in ALLOWED_TAGS:
                tag.unwrap()
                continue
            if tag.name == "h1":
                tag.name = "h2"
            if tag.name == "img":
                image_count += 1
                src = tag.get("src", "")
                alt = " ".join(tag.get("alt", "Illustration").split())[:120] or "Illustration"
                if image_count > MAX_IMAGES_PER_ARTICLE or not src:
                    tag.decompose()
                    continue
                tag.attrs = {"src": src, "alt": alt}
            else:
                tag.attrs = {}
        return soup

    @staticmethod
    def _parts(soup: BeautifulSoup) -> list[str]:
        nodes = list(soup.contents)
        if len(soup.get_text(" ", strip=True).split()) <= LONG_ARTICLE_WORDS:
            return [str(soup)]
        parts: list[list[object]] = [[]]
        words = 0
        for node in nodes:
            node_words = len(node.get_text(" ", strip=True).split()) if hasattr(node, "get_text") else 0
            if getattr(node, "name", None) == "h2" and words >= LONG_ARTICLE_WORDS and parts[-1]:
                parts.append([])
                words = 0
            parts[-1].append(node)
            words += node_words
        return ["".join(str(node) for node in part) for part in parts if part]

    @staticmethod
    def _embed_images(book: epub.EpubBook, soup: BeautifulSoup, article_index: int, allowance: int) -> int:
        """Download a few meaningful images and normalize them for the X4."""
        if allowance <= 0 or importlib.util.find_spec("PIL") is None:
            for image in soup.find_all("img"):
                image.decompose()
            return 0
        import requests
        from PIL import Image

        embedded = 0
        for source_image in list(soup.find_all("img")):
            if embedded >= min(MAX_IMAGES_PER_ARTICLE, allowance):
                source_image.decompose()
                continue
            src = source_image.get("src", "")
            # Tiny/icon-like images and data URLs are normally tracking or chrome.
            if not src.startswith(("http://", "https://")):
                source_image.decompose()
                continue
            try:
                response = requests.get(src, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                image = Image.open(io.BytesIO(response.content))
                if image.width < 120 or image.height < 80:
                    source_image.decompose()
                    continue
                image = image.convert("L")
                image.thumbnail((IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT))
                diagram = bool(re.search(r"chart|diagram|graph|screenshot", source_image.get("alt", ""), re.I))
                extension, media_type = ("png", "image/png") if diagram else ("jpg", "image/jpeg")
                output = io.BytesIO()
                if diagram:
                    image.save(output, format="PNG", optimize=True)
                else:
                    image.save(output, format="JPEG", quality=72, optimize=True, progressive=False)
                embedded += 1
                file_name = f"images/article_{article_index:02d}_{embedded}.{extension}"
                book.add_item(epub.EpubItem(uid=f"article-image-{article_index}-{embedded}", file_name=file_name, media_type=media_type, content=output.getvalue()))
                source_image["src"] = file_name
            except (requests.RequestException, OSError, ValueError):
                source_image.decompose()
        return embedded

    @staticmethod
    def _date_label(value: str) -> str:
        if not value:
            return "Recent"
        try:
            return parsedate_to_datetime(value).strftime("%b %-d, %Y")
        except (TypeError, ValueError, OverflowError):
            return value[:24]

    def _add_qr(self, book: epub.EpubBook, url: str, index: int) -> str:
        if not self.article_qr_codes or not url or importlib.util.find_spec("qrcode") is None:
            return ""
        import qrcode

        image = qrcode.make(url).convert("L").resize((QR_SIZE, QR_SIZE))
        data = io.BytesIO()
        image.save(data, format="PNG", optimize=True)
        file_name = f"images/qr_{index:02d}.png"
        book.add_item(epub.EpubItem(uid=f"qr-{index}", file_name=file_name, media_type="image/png", content=data.getvalue()))
        return f'<hr/><p><strong>Continue on the web</strong></p><img src="{file_name}" alt="QR code for original article"/>'

    def build_epub(self, articles: list[dict[str, str]]) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.date.today().isoformat()
        book_title = f"GenAI Reader - {today}"
        book = epub.EpubBook()
        book.set_identifier(f"{BOOK_PREFIX}-{today}")
        book.set_title(book_title)
        book.set_language(BOOK_LANGUAGE)
        book.add_author(BOOK_AUTHOR)
        book.add_item(epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=CROSSPOINT_CSS))

        selected, total_words = [], 0
        for article in sorted(articles, key=lambda item: (item.get("category", "Other").casefold(), item.get("published", ""))):
            words = len(BeautifulSoup(article.get("content", ""), "html.parser").get_text(" ").split())
            if len(selected) >= MAX_ARTICLES or total_words + words > MAX_TOTAL_WORDS:
                continue
            selected.append(article)
            total_words += words

        spine, toc = ["nav"], []
        chapter_number = 0
        total_images = 0
        for article_index, article in enumerate(selected, start=1):
            title = article.get("title", "Untitled")
            source = article.get("source", "Unknown source")
            category = article.get("category", "Other").split("&", 1)[0].strip()
            clean = self._sanitize(article.get("content", ""))
            # Reserve room for later QR codes while never exceeding the global cap.
            total_images += self._embed_images(book, clean, article_index, MAX_TOTAL_IMAGES - total_images)
            parts = self._parts(clean)
            qr = self._add_qr(book, article.get("url", ""), article_index) if total_images < MAX_TOTAL_IMAGES else ""
            if qr:
                total_images += 1
            minutes = max(1, round(len(clean.get_text(" ").split()) / 225))
            meta = f"{html.escape(source.upper())}<br/>{html.escape(self._date_label(article.get('published', '')))} · {minutes} min"
            for part_index, body in enumerate(parts, start=1):
                chapter_number += 1
                part_suffix = f" — Part {part_index}" if len(parts) > 1 else ""
                full_title = title + part_suffix
                menu_title = _trim_title(f"{category} · {full_title}")
                ending = qr if part_index == len(parts) else ""
                content = (
                    "<html xmlns='http://www.w3.org/1999/xhtml'><head>"
                    f"<title>{html.escape(full_title)}</title><link rel='stylesheet' href='style.css'/></head>"
                    f"<body><h1>{html.escape(full_title)}</h1><p class='article-meta'>{meta}</p>{body}{ending}</body></html>"
                )
                chapter = epub.EpubHtml(title=menu_title, file_name=f"chap_{chapter_number:03d}.xhtml", lang=BOOK_LANGUAGE)
                chapter.set_content(content.encode("utf-8"))
                book.add_item(chapter)
                spine.append(chapter)
                toc.append(chapter)

        book.spine = spine
        book.toc = toc
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        out_path = self.output_dir / f"{BOOK_PREFIX}-{today}.epub"
        epub.write_epub(str(out_path), book)
        return out_path
