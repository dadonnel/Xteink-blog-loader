#!/usr/bin/env python3
import os
import datetime
import feedparser
import requests
import xml.etree.ElementTree as ET
from ebooklib import epub
import shutil
from bs4 import BeautifulSoup
import ssl

# Fix for some SSL errors on older setups
if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context

OUTPUT_DIR = "storage/downloads/rss_epub/output_epubs"
SOURCES_FILE = "storage/downloads/rss_epub/feeds.opml"  # Changed to OPML
DAYS_BACK = 3

# Set User-Agent globally for feedparser
feedparser.USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

def load_sources(path=SOURCES_FILE):
    """Parses an OPML file to extract feed URLs."""
    if not os.path.exists(path):
        print(f"ERROR: {path} not found. Make sure {SOURCES_FILE} is in the folder.")
        return []
    
    feeds = []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        
        # In OPML, feeds are <outline> tags with an 'xmlUrl' attribute.
        # We search recursively (.//) to find them inside category folders.
        for outline in root.findall(".//outline[@xmlUrl]"):
            # Prefer 'text' or 'title' for the name
            name = outline.get("text") or outline.get("title") or "Untitled Feed"
            url = outline.get("xmlUrl")
            
            if url:
                feeds.append({"name": name, "url": url})
                
        print(f"Loaded {len(feeds)} feeds from OPML.")
        
    except Exception as e:
        print(f"ERROR parsing OPML file: {e}")
        return []
        
    return feeds

def is_recent(entry, days_back=DAYS_BACK):
    # If no date, assume it's recent (better to have duplicates than miss content)
    if not hasattr(entry, "published_parsed") and not hasattr(entry, "updated_parsed"):
        return True, "No date found (defaulting to True)"
        
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    
    if not parsed:
        return True, "Date unparseable (defaulting to True)"

    try:
        entry_dt = datetime.datetime(*parsed[:6], tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = (now - entry_dt).days
        if delta <= days_back:
            return True, f"{delta} days ago"
        return False, f"{delta} days ago (Too old)"
    except Exception as e:
        return True, f"Date error: {e} (defaulting to True)"

def fetch_weekly_urls(feeds):
    urls = []
    for feed in feeds:
        try:
            print(f"Fetching {feed['name']}...")
            parsed = feedparser.parse(feed["url"])
            # Check if feed has entries (valid RSS/Atom feed)
            if hasattr(parsed, 'entries') and len(parsed.entries) > 0:
                recent_count = 0
                for entry in parsed.entries:
                    recent, msg = is_recent(entry)
                    if recent:
                        link = entry.get('link', '')
                        title = entry.get('title', link or 'Untitled')
                        urls.append({"title": title, "url": link, "source": feed["name"]})
                        recent_count += 1
                print(f"  {recent_count} recent posts")
            else:
                print(f"  Invalid RSS or no entries: {feed['url']}")
        except Exception as e:
            print(f"  Feed error {feed['name']}: {e}")
    
    # dedupe
    seen = set()
    unique = [item for item in urls if item["url"] not in seen and not seen.add(item["url"])]
    return unique

def clean_html_strict(soup):
    """Attempt to extract clean paragraphs and headers only."""
    target_tags = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'ul', 'ol', 'li']
    parts = []
    
    # Locate main content container
    main_selectors = ['article', 'main', '.post', '.entry-content', '.article-body', '[role="main"]', '.content', '#content']
    content = None
    for selector in main_selectors:
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) > 200:
            content = found
            break
    if not content:
        content = soup

    for elem in content.find_all(target_tags):
        # Don't grab elements inside other elements we already grabbed
        if elem.find_parent(target_tags):
            continue
        # Strip attributes
        if hasattr(elem, 'attrs'):
            elem.attrs = {k: v for k, v in elem.attrs.items() if k in ['href', 'src', 'alt']}
        parts.append(str(elem))
        
    return '\n'.join(parts)

def clean_html_fallback(soup):
    """If strict fails, just strip script/style and return what's left."""
    for tag in soup(['script', 'style', 'nav', 'footer', 'iframe']):
        tag.decompose()
    return str(soup)

def fetch_and_extract(url):
    try:
        headers = {"User-Agent": feedparser.USER_AGENT}
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
    except Exception as e:
        print(f"  ! Network Fail: {e}")
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    title = soup.title.string.strip() if soup.title and soup.title.string else 'Untitled'

    # Remove strict garbage
    for unwanted in soup(["script", "style", "nav", "footer", "header", "aside", "form", "button", "iframe"]):
        unwanted.decompose()
        
    # Attempt Strict Cleaning
    clean_content = clean_html_strict(soup)
    
    # Fallback if content is too short (likely failed to find 'p' tags)
    if len(clean_content) < 500:
        print("    (Strict cleaning returned little text, using fallback...)")
        clean_content = clean_html_fallback(soup)

    if len(clean_content) < 300:
        print(f"    ! Content too short ({len(clean_content)} chars). Skipping.")
        return None

    header = f"""
    <div style="margin-bottom: 2em; border-bottom: 1px solid #ccc; padding-bottom: 1em;">
        <h1 style="margin:0;">{title}</h1>
        <p style="font-size: 0.8em; color: #666;">
            Source: <a href="{url}">{url}</a>
        </p>
    </div>
    """
    return header + clean_content

def build_epub(articles, out_dir=OUTPUT_DIR):
    if not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    today = datetime.date.today().isoformat()
    book_title = f"GenAI Weekly - {today}"
    
    book = epub.EpubBook()
    book.set_identifier(f"genai-weekly-{today}")
    book.set_title(book_title)
    book.set_language("en")
    book.add_author("GenAI Weekly")

    # High Contrast E-ink CSS
    style = """
    body { font-family: 'Georgia', serif; font-size: 1.1em; line-height: 1.5; margin: 0; padding: 3%; color: #000; background: #fff; }
    h1, h2, h3 { font-family: sans-serif; margin-top: 1.5em; color: #000; border-bottom: 1px solid #eee; }
    p { margin: 0 0 1em 0; text-align: left; }
    pre { background: #f4f4f4; padding: 1em; overflow-x: auto; font-size: 0.9em; border: 1px solid #ccc; }
    img { max-width: 100%; height: auto; filter: grayscale(100%); }
    blockquote { border-left: 4px solid #333; padding-left: 1em; margin: 1em; font-style: italic; }
    """
    book.add_item(epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=style))

    spine = ["nav"]
    toc = []

    for idx, art in enumerate(articles, start=1):
        safe_title = art["title"][:80] + "..." if len(art["title"]) > 80 else art["title"]
        # Basic HTML wrapper
        c_content = f"<html><head><title>{safe_title}</title><link rel='stylesheet' href='style.css'/></head><body>{art['content']}</body></html>"
        
        c = epub.EpubHtml(title=safe_title, file_name=f"chap_{idx:02d}.xhtml", lang="en")
        c.set_content(c_content.encode('utf-8'))
        book.add_item(c)
        spine.append(c)
        toc.append(c)

    book.spine = spine
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    out_path = os.path.join(out_dir, f"genai-weekly-{today}.epub")
    epub.write_epub(out_path, book)
    return out_path

def main():
    print("--- GenAI Weekly Generator ---")
    feeds = load_sources()
    if not feeds:
        print("No feeds loaded.")
        return

    urls = fetch_weekly_urls(feeds)
    print(f"\nTotal unique URLs found: {len(urls)}")
    
    if not urls:
        print("No recent articles found in any feed.")
        return

    articles = []
    for meta in urls:
        print(f"Processing: {meta['title'][:40]}...")
        extracted = fetch_and_extract(meta["url"])
        if extracted:
            articles.append({"title": meta["title"], "url": meta["url"], "source": meta["source"], "content": extracted})
            print("  OK")
        else:
            print("  Failed")

    if not articles:
        print("\nERROR: Found URLs but failed to extract content from all of them.")
        return

    print(f"\nBuilding EPUB with {len(articles)} articles...")
    out_path = build_epub(articles)
    print(f"Success! EPUB saved to: {out_path}")
    
    sync_dir = os.path.join(OUTPUT_DIR, "xteink_sync")
    os.makedirs(sync_dir, exist_ok=True)
    xteink_path = os.path.join(sync_dir, os.path.basename(out_path))
    shutil.copy2(out_path, xteink_path)
    print(f"Synced to: {xteink_path}")

if __name__ == "__main__":
    main()
