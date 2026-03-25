import unittest
from unittest.mock import patch

import feed_service
from feed_discovery import FeedResolution


class ValidateFeedsOrderingTests(unittest.TestCase):
    def test_preserves_input_order_with_duplicate_feed_names(self):
        feeds = [
            {"name": "Same Name", "url": "https://example.com/1.xml"},
            {"name": "Same Name", "url": "https://example.com/2.xml"},
            {"name": "Different", "url": "https://example.com/3.xml"},
        ]

        def fake_fetch(feed, _timeout_s):
            return feed_service.FeedValidationResult(
                feed=feed["url"],
                status="ok",
                counts={"1 day": 0, "7 days": 0, "30 days": 0},
            )

        with patch("feed_service._fetch_feed", side_effect=fake_fetch):
            results = feed_service.validate_feeds(feeds, max_workers=3)

        self.assertEqual(
            [result.feed for result in results],
            [feed["url"] for feed in feeds],
        )

    def test_auto_discover_invalid_feeds_retries_with_resolved_feed(self):
        feed = {"name": "Example", "url": "https://example.com/post/one"}
        invalid = feed_service.FeedValidationResult(
            feed="Example",
            status="error",
            counts={"1 day": 0, "7 days": 0, "30 days": 0},
            reason="invalid feed",
        )
        valid = feed_service.FeedValidationResult(
            feed="Example",
            status="ok",
            counts={"1 day": 1, "7 days": 2, "30 days": 3},
        )

        with (
            patch("feed_service._fetch_feed", side_effect=[invalid, valid]) as fetch_mock,
            patch(
                "feed_service.resolve_feed",
                return_value=FeedResolution(
                    name="Example",
                    feed_url="https://example.com/feed.xml",
                    category="Example",
                    feed_type="rss",
                ),
            ) as resolve_mock,
        ):
            result = feed_service.validate_feeds([feed], auto_discover_invalid_feeds=True)[0]

        self.assertEqual(result.status, "ok")
        self.assertEqual(fetch_mock.call_count, 2)
        resolve_mock.assert_called_once_with("https://example.com/post/one")


if __name__ == "__main__":
    unittest.main()
