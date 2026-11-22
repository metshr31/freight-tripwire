"""Isolated synthetic primary-source test; sends exactly one clearly marked ntfy test."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import watcher


def main():
    config = watcher.load_json(watcher.WATCHLIST_FILE, None)
    spec = {"id": "synthetic-primary-test", "company": "Tripwire TEST", "role": "service",
            "url": "https://example.org/alerts", "kind": "html", "track_updates": True}
    baseline = '<main><article><a href="/alerts/baseline">TEST ONLY: existing baseline item</a></article></main>'
    update = baseline.replace('</main>', '<article><a href="/alerts/new">TEST ONLY: primary-source detection and delivery check; no real freight event</a><p>Synthetic test of direct monitoring, saved state, and duplicate suppression.</p></article></main>')
    update = update.replace("</article></main>", f'<time datetime="{watcher.now()}"></time></article></main>')
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(watcher, "STATE_FILE", Path(directory, "state.json")), patch.object(watcher, "HEALTH_FILE", Path(directory, "health.json")), patch.object(watcher, "LOG_FILE", Path(directory, "alerts.jsonl")), patch.object(watcher, "expanded_sources", return_value=[("primary:synthetic-primary-test", spec["url"], spec)]):
            with patch.object(watcher, "fetch", return_value=baseline):
                assert watcher.run(config) == 0
                assert not watcher.LOG_FILE.exists(), "Baseline unexpectedly generated a notification"
            with patch.object(watcher, "fetch", return_value=update):
                assert watcher.run(config) == 0
                assert len(watcher.LOG_FILE.read_text().splitlines()) == 1, "New item was not delivered/logged"
                assert watcher.run(config) == 0
                assert len(watcher.LOG_FILE.read_text().splitlines()) == 1, "Duplicate alert was generated"
            saved = json.loads(watcher.STATE_FILE.read_text())
            assert sum(r["status"] == "sent" for r in saved["seen"].values()) == 1
    print("PASS: quiet primary baseline -> new item -> ntfy acceptance -> durable state -> no duplicate")


if __name__ == "__main__":
    main()
