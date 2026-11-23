"""Unit tests for watcher.py HTTP headers, curl_cffi fallback, retries, and SEC skip."""
import http.client
import io
import json
import logging
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import watcher


class WatcherFetchTests(unittest.TestCase):
    def test_browser_headers_sent_for_non_sec_requests(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _n=None):
                return b"<html>ok</html>"

        def fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            body = watcher.fetch("https://example.com/news")

        self.assertEqual(body, "<html>ok</html>")
        self.assertIn("mozilla/5.0", captured["headers"]["user-agent"].lower())
        self.assertIn("accept-language", captured["headers"])
        self.assertEqual(captured["headers"].get("upgrade-insecure-requests"), "1")

    def test_curl_cffi_fallback_on_403_and_429(self):
        for code in (403, 429):
            with self.subTest(status=code):
                err = urllib.error.HTTPError(
                    "https://example.com/feed", code, f"HTTP {code}", {}, io.BytesIO(b"")
                )
                with patch("urllib.request.urlopen", side_effect=err), patch.object(
                    watcher, "_fetch_curl_cffi", return_value="<rss>ok</rss>"
                ) as mock_cffi:
                    body = watcher.fetch("https://example.com/feed", attempts=2)
                self.assertEqual(body, "<rss>ok</rss>")
                mock_cffi.assert_called_once()

    def test_timeout_retry_with_exponential_backoff(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _n=None):
                return b"ok-after-retry"

        with patch(
            "urllib.request.urlopen", side_effect=[TimeoutError("timed out"), FakeResponse()]
        ), patch("time.sleep") as mock_sleep:
            body = watcher.fetch("https://example.com/slow", timeout=20, attempts=2)

        self.assertEqual(body, "ok-after-retry")
        mock_sleep.assert_called_once_with(1)


    def test_curl_cffi_fallback_on_520_and_remote_disconnected(self):
        err_520 = urllib.error.HTTPError(
            "https://example.com/feed", 520, "HTTP 520", {}, io.BytesIO(b"")
        )
        with patch("urllib.request.urlopen", side_effect=err_520), patch.object(
            watcher, "_fetch_curl_cffi", return_value="<rss>ok-520</rss>"
        ) as mock_cffi:
            self.assertEqual(watcher.fetch("https://example.com/feed", attempts=2), "<rss>ok-520</rss>")
            mock_cffi.assert_called_once()

        with patch(
            "urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("Remote end closed connection without response"),
        ), patch.object(watcher, "_fetch_curl_cffi", return_value="<rss>ok-rd</rss>") as mock_cffi:
            self.assertEqual(watcher.fetch("https://example.com/feed", attempts=2), "<rss>ok-rd</rss>")
            mock_cffi.assert_called_once()

    def test_load_official_stb_sends_form_content_type(self):
        html = "<table class=\"stb-ajax-table\" data-stb-action=\"stb_load\" data-stb-nonce=\"abc\"><tbody></tbody></table>"
        ajax = json.dumps({"success": True, "data": {"rows": "<tr><td>11/23/2026</td><td>FD 36873</td><td>123456</td><td>Test Filing</td></tr>"}})
        calls = []

        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            return ajax if "admin-ajax.php" in url else html

        with patch.object(watcher, "fetch", side_effect=fake_fetch):
            entries = watcher.load_official_stb("https://www.stb.gov/proceedings-actions/filings/")

        self.assertEqual(len(entries), 1)
        self.assertEqual(calls[1][1]["request_headers"]["Content-Type"], "application/x-www-form-urlencoded")

    def test_collect_primary_uses_higher_timeout_and_retry(self):
        spec = {
            "id": "kenco-test",
            "company": "Kenco",
            "role": "news",
            "url": "https://kencogroup.com/category/news/feed/",
            "kind": "rss",
        }
        rss = "<rss><channel><item><title>Kenco Opens Facility</title><link>https://kencogroup.com/a</link></item></channel></rss>"
        with patch.object(watcher, "fetch", return_value=rss) as mock_fetch:
            entries, err = watcher.collect_source(("primary:kenco-test", spec["url"], spec))

        self.assertIsNone(err)
        self.assertEqual(len(entries), 1)
        _, kwargs = mock_fetch.call_args
        self.assertGreaterEqual(kwargs["timeout"], 20)
        self.assertGreaterEqual(kwargs["attempts"], 2)


class WatcherSecAndConfigTests(unittest.TestCase):
    def test_sec_sources_skipped_gracefully_when_env_missing(self):
        cfg = watcher.load_json(watcher.WATCHLIST_FILE, None)
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir, "seen.json")
            health_path = Path(tmpdir, "health.json")
            with patch.object(watcher, "STATE_FILE", state_path), patch.object(
                watcher, "HEALTH_FILE", health_path
            ), patch.dict(os.environ, {"SEC_USER_AGENT": ""}, clear=False), patch.object(
                watcher,
                "expanded_sources",
                return_value=[
                    ("sec:0", "https://www.sec.gov/0", False),
                    ("sec:100", "https://www.sec.gov/100", False),
                    ("sec:200", "https://www.sec.gov/200", False),
                    ("https://example.com/rss", "https://example.com/rss", False),
                ],
            ), patch.object(
                watcher,
                "collect_completed",
                return_value=[
                    (
                        ("https://example.com/rss", "https://example.com/rss", False),
                        ([{"title": "Test Item", "link": "https://example.com/1", "source": "https://example.com/rss"}], None),
                    )
                ],
            ), self.assertLogs("freight-tripwire", level=logging.INFO) as logs:
                rc = watcher.run(cfg, dry_run=True)

        self.assertEqual(rc, 0)
        sec_info = [m for m in logs.output if "SEC_USER_AGENT" in m]
        self.assertEqual(len(sec_info), 1)
        self.assertNotIn("WARNING", sec_info[0])

    def test_dead_community_feeds_removed_from_watchlist(self):
        cfg = watcher.load_json(watcher.WATCHLIST_FILE, None)
        feeds = set(cfg["rss_feeds"])
        self.assertNotIn("https://www.reddit.com/r/Truckers/new/.rss", feeds)
        self.assertNotIn("https://www.truckingboards.com/bb/forums/-/index.rss", feeds)


if __name__ == "__main__":
    unittest.main()