from datetime import datetime, timedelta, timezone
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from freshness import timestamp, verdict, publication_from_page


class FreshnessTests(unittest.TestCase):
    def test_late_old_story_and_old_retry_never_publish(self):
        import watcher
        from test_watcher import CONFIG
        config = {**CONFIG, "max_alert_age_minutes": 120}
        old = {"title": "Union Pacific launches new service", "published": "2020-01-01T12:00:00Z",
               "source": "https://carrier.example/rss", "link": "https://carrier.example/old"}
        for queued in [False, True]:
            with self.subTest(queued=queued), tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory, "state.json")
                seen = {"old-retry": {"status": "pending", "first_seen": watcher.now(), "entry": old,
                                      "label": ["P2", "Union Pacific", "Official company source"]}} if queued else {}
                watcher.atomic_save(state_file, {"version": 2, "seen": seen, "sources": {"test": {"seeded": True, "failures": 0}}})
                source = ("test", old["source"], False)
                with patch.object(watcher, "STATE_FILE", state_file), patch.object(watcher, "HEALTH_FILE", Path(directory, "health.json")), patch.object(watcher, "LOG_FILE", Path(directory, "alerts.jsonl")), patch.object(watcher, "expanded_sources", return_value=[source]), patch.object(watcher, "collect_completed", return_value=iter([(source, ([old], None))])), patch.object(watcher, "publish") as publish:
                    self.assertEqual(watcher.run(config), 0)
                    publish.assert_not_called()
                    health = json.loads(watcher.HEALTH_FILE.read_text())
                    self.assertGreaterEqual(health["freshness_holds"]["stale"], 1)
                    self.assertEqual(health["queued_alerts"], 0)

    def test_publication_not_discovery_controls_delivery(self):
        at = datetime(2026, 11, 22, 20, tzinfo=timezone.utc)
        for age in [2*24*60, 7*24*60, 14*24*60, 121]:
            item = {"published": (at-timedelta(minutes=age)).isoformat(), "first_seen": at.isoformat()}
            self.assertEqual(verdict(item, 120, at), "stale")
        self.assertEqual(verdict({"published": (at-timedelta(minutes=20)).isoformat()}, 120, at), "fresh")

    def test_undated_date_only_naive_and_future_are_held(self):
        at = datetime(2026, 11, 22, 20, tzinfo=timezone.utc)
        for value in [None, "", "2026-11-22", "22 noviembre 2026", "2026-11-22T19:50:00"]:
            self.assertEqual(verdict({"published": value}, 120, at), "unverified_publication_time")
        self.assertEqual(verdict({"published": "2026-11-22T21:00:00Z"}, 120, at), "future_publication_time")

    def test_atom_updated_timestamp_is_not_new_publication(self):
        import watcher
        items = watcher.rss_items('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Old story edited today</title><link href="https://carrier.example/old"/><updated>2026-11-22T20:00:00Z</updated></entry></feed>', 'source')
        self.assertIsNone(timestamp(items[0]["published"]))

    def test_explicit_timezones_and_rss_dates(self):
        self.assertEqual(timestamp("Sun, 22 Nov 2026 15:00:00 -0500"), timestamp("2026-11-22T20:00:00Z"))

    def test_original_publisher_metadata_not_modified_or_footer_date(self):
        self.assertEqual(publication_from_page('<meta property="article:published_time" content="2026-11-22T15:00:00-05:00">'), "2026-11-22T15:00:00-05:00")
        self.assertIsNone(publication_from_page('<meta property="article:modified_time" content="2026-11-22T20:00:00Z"><footer>2026-11-22</footer>'))
        self.assertIsNone(publication_from_page('<script type="application/ld+json">{"@type":"Organization","datePublished":"2026-11-22T20:00:00Z"}</script>'))
        self.assertIsNone(publication_from_page('<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","url":"https://carrier.example/other","datePublished":"2026-11-22T20:00:00Z"}]}</script>', "https://carrier.example/story"))


if __name__ == "__main__":
    unittest.main()
