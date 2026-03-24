import feed_discovery


class FakeResponse:
    def __init__(self, *, body: str, content_type: str, url: str):
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": content_type}
        self._url = url

    def read(self, *_args, **_kwargs):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_resolve_feed_from_html_post_url(monkeypatch):
    html_doc = """
    <html>
      <head>
        <title>Example Site</title>
        <link rel=\"alternate\" type=\"application/rss+xml\" href=\"/feed.xml\" />
      </head>
      <body>Post</body>
    </html>
    """
    rss_doc = """<?xml version=\"1.0\"?><rss><channel><title>Example Engineering</title></channel></rss>"""

    responses = {
        "https://example.com/posts/one": FakeResponse(
            body=html_doc,
            content_type="text/html; charset=utf-8",
            url="https://example.com/posts/one",
        ),
        "https://example.com/feed.xml": FakeResponse(
            body=rss_doc,
            content_type="application/rss+xml",
            url="https://example.com/feed.xml",
        ),
    }

    monkeypatch.setattr(
        feed_discovery,
        "urlopen",
        lambda request, timeout=10: responses[request.full_url],
    )

    result = feed_discovery.resolve_feed(
        "https://example.com/posts/one",
        ["Engineering & Code", "AI & Research"],
    )

    assert result.feed_url == "https://example.com/feed.xml"
    assert result.feed_type == "rss"
    assert result.name == "Example Engineering"
    assert result.category == "Engineering & Code"


def test_resolve_feed_detects_json_feed(monkeypatch):
    json_feed = '{"version":"https://jsonfeed.org/version/1.1","title":"JSON Feed","items":[]}'

    monkeypatch.setattr(
        feed_discovery,
        "urlopen",
        lambda request, timeout=10: FakeResponse(
            body=json_feed,
            content_type="application/feed+json",
            url=request.full_url,
        ),
    )

    result = feed_discovery.resolve_feed("https://news.example.com/feed.json", ["AI & Research"])

    assert result.feed_type == "json"
    assert result.name == "JSON Feed"
    assert result.category == "News"
