from pathlib import Path
import sys
import types

import pytest

pytest.importorskip("bs4")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if "feedparser" not in sys.modules:
    sys.modules["feedparser"] = types.SimpleNamespace(USER_AGENT="test-agent", parse=lambda _url: None)

if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(get=lambda *args, **kwargs: None)

from rss_epub.feed_service import FeedService


def test_remove_trailing_boilerplate_strips_marketing_footer_and_year_archive():
    html = """
    <p>Real article paragraph one with useful content.</p>
    <p>Real article paragraph two with useful content.</p>
    <h2>Monthly briefing</h2>
    <p>Sponsor me for $10/month and get a curated email digest.</p>
    <ul>
      <li><a href='/disclosures'>Disclosures</a></li>
      <li><a href='/colophon'>Colophon</a></li>
      <li><a href='/2002'>2002</a></li>
      <li><a href='/2003'>2003</a></li>
      <li><a href='/2004'>2004</a></li>
      <li><a href='/2005'>2005</a></li>
      <li><a href='/2006'>2006</a></li>
    </ul>
    """

    cleaned = FeedService.remove_trailing_boilerplate(html)

    assert "Real article paragraph one" in cleaned
    assert "Monthly briefing" not in cleaned
    assert "Disclosures" not in cleaned
    assert ">2002<" not in cleaned


def test_remove_trailing_boilerplate_keeps_normal_article_content():
    html = """
    <h2>Wrapping up 2024</h2>
    <p>This article references years and trends but is not a footer.</p>
    <ul>
      <li>2024 roadmap item one</li>
      <li>2025 roadmap item two</li>
    </ul>
    """

    cleaned = FeedService.remove_trailing_boilerplate(html)

    assert "Wrapping up 2024" in cleaned
    assert "roadmap item one" in cleaned
