import unittest
from unittest.mock import patch

import feed_service


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


if __name__ == "__main__":
    unittest.main()
