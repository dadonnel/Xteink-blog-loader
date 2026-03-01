"""EPUB creation logic for article bundles."""

from __future__ import annotations

import datetime
from pathlib import Path

from ebooklib import epub

from .config import BOOK_AUTHOR, BOOK_LANGUAGE, BOOK_PREFIX, OUTPUT_DIR


class EpubService:
    def __init__(self, output_dir: Path = OUTPUT_DIR):
        self.output_dir = Path(output_dir)

    def build_epub(self, articles: list[dict[str, str]]) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.date.today().isoformat()
        book_title = f"GenAI Weekly - {today}"

        book = epub.EpubBook()
        book.set_identifier(f"{BOOK_PREFIX}-{today}")
        book.set_title(book_title)
        book.set_language(BOOK_LANGUAGE)
        book.add_author(BOOK_AUTHOR)

        style = """
    body { font-family: 'Georgia', serif; font-size: 1.1em; line-height: 1.5; margin: 0; padding: 3%; color: #000; background: #fff; }
    h1, h2, h3 { font-family: sans-serif; margin-top: 1.5em; color: #000; border-bottom: 1px solid #eee; }
    p { margin: 0 0 1em 0; text-align: left; }
    pre { background: #f4f4f4; padding: 1em; overflow-x: auto; font-size: 0.9em; border: 1px solid #ccc; }
    img { max-width: 100%; height: auto; filter: grayscale(100%); }
    blockquote { border-left: 4px solid #333; padding-left: 1em; margin: 1em; font-style: italic; }
    """
        book.add_item(
            epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=style)
        )

        spine = ["nav"]
        toc = []

        for idx, art in enumerate(articles, start=1):
            safe_title = art["title"][:80] + "..." if len(art["title"]) > 80 else art["title"]
            c_content = (
                f"<html><head><title>{safe_title}</title><link rel='stylesheet' href='style.css'/></head>"
                f"<body>{art['content']}</body></html>"
            )

            chapter = epub.EpubHtml(title=safe_title, file_name=f"chap_{idx:02d}.xhtml", lang=BOOK_LANGUAGE)
            chapter.set_content(c_content.encode("utf-8"))
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
