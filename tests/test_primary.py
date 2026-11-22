import json
import tempfile
import threading
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher
from primary import html_items, decorate, inline_items, document_items, central_items
from test_watcher import CONFIG


class PrimarySourceTests(unittest.TestCase):
    def test_central_public_api_empty_success_and_schema_validation(self):
        spec = {"url": "https://centraltransport.com/company/news.aspx"}
        self.assertEqual(central_items('{"d": []}', spec), [])
        item = central_items(json.dumps({"d": [{"ItemId": 12, "ArticleTitle": "New service center opens", "ArticleHtml": "<p>New Ohio terminal.</p>", "DateToBePublished": "/Date(1795348800000)/"}]}), spec)[0]
        self.assertEqual(item["link"], "https://centraltransport.com/company/news?id=12")
        self.assertEqual(item["summary"], "New Ohio terminal.")
        with self.assertRaises(ValueError):
            central_items('{"error": "Unavailable"}', spec)

    def test_inline_release_ids_remain_distinct_on_one_page(self):
        spec = {"url": "https://carrier.example/releases", "selector": "p.bold[id]"}
        html = '<p class="bold" id="one">First terminal opens Monday</p><p>New service in Ohio.</p><p class="bold" id="two">Second terminal opens Tuesday</p><p>New service in Maryland.</p>'
        items = inline_items(html, spec)
        self.assertEqual(len(items), 2)
        self.assertNotEqual(watcher.item_key(items[0]), watcher.item_key(items[1]))
        self.assertNotIn("Maryland", items[0]["summary"])

    def test_document_release_title_comes_from_its_own_intro(self):
        spec = {"url": "https://carrier.example/news"}
        html = '<p><strong>Third Quarter Earnings Release</strong><em>Nov 1, 2026</em>Revenue increases.</p><p>Read the release <a href="/s/earnings.pdf">Read More &gt;</a></p>'
        item = document_items(html, spec)[0]
        self.assertEqual(item["title"], "Third Quarter Earnings Release")
        self.assertEqual(item["link"], "https://carrier.example/s/earnings.pdf")

    def test_extracts_real_links_and_excludes_navigation(self):
        spec = {"url": "https://carrier.example/news", "company": "Carrier", "role": "news", "id": "carrier"}
        html = '<nav><a href="/news/privacy">Privacy updates</a></nav><main><article><a href="/news/new-terminal"><h2>New terminal opens Monday</h2></a><p>Service expands to three cities.</p><time datetime="2026-11-22">Nov 22</time></article><a href="/news">All news</a><a href="https://unrelated.example/news/new">Unrelated outside story</a></main>'
        items = html_items(html, spec)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "New terminal opens Monday")
        self.assertEqual(items[0]["summary"], "Service expands to three cities.")
        self.assertEqual(items[0]["published"], "2026-11-22")

    def test_company_context_is_taken_from_registered_primary_source(self):
        spec = {"url": "https://carrier.example/alerts", "company": "Union Pacific", "role": "service", "id": "up-service"}
        item = decorate([{"title": "EMP return policy effective Monday", "summary": "", "link": "https://carrier.example/alerts/new", "source": spec["url"]}], spec)[0]
        self.assertEqual(watcher.label_item(item, CONFIG), ("P2", "Union Pacific", "Official company source"))
        self.assertIsNone(watcher.label_item({**item, "title": "FreightWaves announcement"}, CONFIG))

    def test_advisory_revision_at_same_url_is_detectable(self):
        spec = {"url": "https://carrier.example/alerts", "company": "Union Pacific", "role": "service", "id": "up-service", "track_updates": True}
        first = decorate([{"title": "Terminal operating plan", "summary": "Gate closes at 5 PM.", "link": "https://carrier.example/alerts/plan"}], spec)[0]
        second = decorate([{"title": "Terminal operating plan", "summary": "Gate reopens at 8 PM.", "link": "https://carrier.example/alerts/plan"}], spec)[0]
        self.assertNotEqual(watcher.item_key(first), watcher.item_key(second))
        self.assertEqual(watcher.item_key(first), watcher.item_key(dict(first)))

    def test_empty_or_challenge_page_fails_instead_of_claiming_coverage(self):
        spec = {"url": "https://carrier.example/news"}
        for html in ['<title>Access Denied</title><main>Blocked</main>', '<main><a href="/news">News</a></main>']:
            with self.assertRaises(ValueError):
                html_items(html, spec)

    def test_slow_source_does_not_delay_completed_source(self):
        release = threading.Event()
        def collect(spec):
            if spec[0] == "slow":
                release.wait(3)
            return [{"title": spec[0]}], None
        with patch.object(watcher, "collect_source", side_effect=collect):
            results = watcher.collect_completed([("slow", "slow", False), ("fast", "fast", False)])
            try:
                first = next(results)
                self.assertEqual(first[0][0], "fast")
            finally:
                release.set()
                list(results)

    def test_primary_alert_is_sent_and_saved_before_later_sources_finish(self):
        spec = {"id": "up", "company": "Union Pacific", "role": "service", "url": "https://carrier.example/alerts", "kind": "html"}
        item = decorate([{"title": "New gate operating plan", "link": "https://carrier.example/alerts/new", "source": spec["url"], "summary": "Gate closes at 5 PM."}], spec)[0]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.json")
            watcher.atomic_save(state, {"version": 2, "seen": {}, "sources": {"primary:up": {"seeded": True, "failures": 0}}})
            with patch.object(watcher, "STATE_FILE", state), patch.object(watcher, "HEALTH_FILE", Path(directory, "health.json")), patch.object(watcher, "LOG_FILE", Path(directory, "alerts.jsonl")), patch.object(watcher, "publish") as publish:
                specs = [("primary:up", spec["url"], spec), ("slow", "https://slow.example", False)]
                def completed(_):
                    yield specs[0], ([item], None)
                    publish.assert_called_once()
                    saved = json.loads(state.read_text())
                    self.assertEqual(next(iter(saved["seen"].values()))["status"], "sent")
                    yield specs[1], (None, RuntimeError("slow source failed"))
                with patch.object(watcher, "expanded_sources", return_value=specs), patch.object(watcher, "collect_completed", side_effect=completed):
                    self.assertEqual(watcher.run(CONFIG), 0)
                self.assertIn("Gate closes at 5 PM.", publish.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
