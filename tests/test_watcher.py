import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher


CONFIG = {
    "companies": ["Union Pacific", "Amazon"],
    "priority_keywords": ["bankruptcy", "strike", "terminal closure"],
    "material_event_keywords": ["intermodal", "freight", "new service"],
    "exclude_terms": ["freightwaves", "sonar"],
    "freight_context_keywords": ["freight", "rail", "intermodal", "terminal"],
    "ambiguous_company_names": ["Amazon"],
    "rss_feeds": ["https://news.example.org/rss"],
    "stb_docket_feed": "https://docketyard.org/d/FD-36873/feed",
    "stb_official_pages": [],
}


def rss(items):
    return "<rss><channel>" + "".join(
        f"<item><title>{title}</title><link>{link}</link><description>{desc}</description></item>"
        for title, link, desc in items
    ) + "</channel></rss>"


class WatcherTests(unittest.TestCase):
    def test_rss_and_atom(self):
        r = watcher.rss_items(rss([("Union Pacific intermodal lane", "https://example.org/1", "new service")]), "test")
        self.assertEqual(len(r), 1)
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link rel="alternate" href="https://example.org/a"/><summary>Detailed</summary></entry></feed>'
        a = watcher.rss_items(atom, "test")
        self.assertEqual(a[0]["link"], "https://example.org/a")

    def test_classification_and_exclusions(self):
        base = {"title": "Amazon launches new freight service", "link": "https://example.org/a", "summary": "", "source": "press"}
        self.assertEqual(watcher.label_item(base, CONFIG)[0], "P2")
        self.assertIsNone(watcher.label_item({**base, "title": "Amazon new streaming series"}, CONFIG))
        self.assertIsNone(watcher.label_item({**base, "title": "FreightWaves SONAR covers Amazon new freight"}, CONFIG))
        community = {**base, "community": True, "title": "Union Pacific rail strike talk"}
        self.assertEqual(watcher.label_item(community, CONFIG)[0], "TIP")

    def test_docket_id_deduplicates_sources(self):
        a = {"link": "https://docketyard.org/filing/313150"}
        b = {"link": "https://www.stb.gov/a/313150.pdf", "stb_kind": "filing", "stb_id": "313150"}
        self.assertEqual(watcher.item_key(a), watcher.item_key(b))

    def test_official_stb_scoped_rows(self):
        html = '''<html><table><tr><th>Date</th><th>Docket</th><th>ID</th><th>Filer</th></tr>
          <tr><td>11/20/2026</td><td>FD_36873</td><td>313150</td><td>CSX</td><td><a href="/docs/313150.pdf">PDF</a></td></tr>
          <tr><td>11/20/2026</td><td>FD_10000</td><td>313151</td><td>Other</td></tr></table></html>'''
        items = watcher.official_stb_items(html, "https://www.stb.gov/proceedings-actions/filings/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["stb_id"], "313150")

    def test_businesswire_public_release_cards(self):
        html = '<div><a href="/news/home/20261122000001/en/Test"><h2>Union Pacific new intermodal service</h2></a><div class="rich-text">New rail lane opens Monday.</div></div>'
        items = watcher.businesswire_items(html, "https://www.businesswire.com/newsroom/industry/transport")
        self.assertEqual(items[0]["title"], "Union Pacific new intermodal service")
        self.assertIn("opens Monday", items[0]["summary"])
        self.assertEqual(items[0]["link"], "https://www.businesswire.com/news/home/20261122000001/en/Test")
        with self.assertRaisesRegex(ValueError, "no release cards"):
            watcher.businesswire_items('<html>Unavailable</html>', "https://www.businesswire.com/newsroom/industry/transport")

    def test_stb_loads_dynamic_rows(self):
        page = """<table class="stb-ajax-table" data-stb-action="stb_hook_table_filings" data-stb-nonce="test">
        <thead><tr><th></th><th>Official Filing Date</th><th>Docket Number</th><th>Filing ID</th><th>Filed For</th></tr></thead><tbody></tbody></table>"""
        rows = '<tr><td></td><td>11/22/2026</td><td>FD_36873_49</td><td>313056</td><td>CPKC</td></tr>'
        with patch.object(watcher, "fetch", side_effect=[page, json.dumps({"success": True, "data": {"rows": rows}})]) as fetch:
            items = watcher.load_official_stb("https://www.stb.gov/proceedings-actions/filings/")
        self.assertEqual(items[0]["stb_id"], "313056")
        self.assertEqual(items[0]["published"], "11/22/2026")
        self.assertIn(b"per-page=500", fetch.call_args.kwargs["data"])

    def test_stb_uses_record_column_not_docket_number(self):
        html = """<table><thead><tr><th></th><th>Service Date</th><th>Decision ID</th><th>Docket Number</th><th>Title</th></tr></thead>
        <tr><td></td><td>11/22/2026</td><td>53293</td><td>FD 36873</td><td>Decision</td></tr></table>"""
        item = watcher.official_stb_items(html, "https://www.stb.gov/proceedings-actions/decisions/")[0]
        self.assertEqual(item["stb_id"], "53293")

    def test_unloaded_stb_table_is_failure(self):
        with self.assertRaisesRegex(ValueError, "no loaded records"):
            watcher.official_stb_items('<table><thead><tr><th>ID</th></tr></thead><tr><td></td></tr></table>',
                                       "https://www.stb.gov/proceedings-actions/filings/")

    def test_failed_push_remains_queued_for_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (watcher.STATE_FILE, watcher.HEALTH_FILE, watcher.LOG_FILE)
            watcher.STATE_FILE = Path(tmp, "seen_items.json")
            watcher.HEALTH_FILE = Path(tmp, "health.json")
            watcher.LOG_FILE = Path(tmp, "alerts.jsonl")
            baseline = rss([("Starting story", "https://example.org/a", "not important")])
            update = rss([
                ("Starting story", "https://example.org/a", "not important"),
                ("Union Pacific terminal closure", "https://example.org/b", "terminal closure")
            ])
            try:
                with patch.object(watcher, "expanded_sources", return_value=[("news", "https://news.example.org/rss", False)]):
                    with patch.object(watcher, "fetch", return_value=baseline):
                        with patch.object(watcher, "publish") as pub:
                            self.assertEqual(watcher.run(CONFIG), 0)
                            pub.assert_not_called()  # source-specific quiet baseline
                    with patch.object(watcher, "fetch", return_value=update):
                        with patch.object(watcher, "publish", side_effect=RuntimeError("push down")):
                            self.assertEqual(watcher.run(CONFIG), 0)
                    state = json.loads(watcher.STATE_FILE.read_text())
                    self.assertEqual(sum(x["status"] == "pending" for x in state["seen"].values()), 1)
                    with patch.object(watcher, "fetch", return_value=update):
                        with patch.object(watcher, "publish") as pub:
                            self.assertEqual(watcher.run(CONFIG), 0)
                            self.assertEqual(pub.call_count, 1)
                    state = json.loads(watcher.STATE_FILE.read_text())
                    self.assertEqual(sum(x["status"] == "pending" for x in state["seen"].values()), 0)
                    self.assertEqual(len(watcher.LOG_FILE.read_text().splitlines()), 1)
            finally:
                watcher.STATE_FILE, watcher.HEALTH_FILE, watcher.LOG_FILE = old


if __name__ == "__main__":
    unittest.main()
