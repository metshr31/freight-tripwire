#!/usr/bin/env python3
"""Freight Tripwire: lightweight, auditable freight leads (not a verification engine)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "seen_items.json"
HEALTH_FILE = BASE / "health.json"
LOG_FILE = BASE / "alerts.jsonl"
WATCHLIST_FILE = BASE / "watchlist.json"
MAX_SEEN = 16000
MAX_ALERTS_PER_RUN = 20
LOG = logging.getLogger("freight-tripwire")
UTC = timezone.utc
USER_AGENT = "FreightTripwire/1.1 (news monitoring; public sources)"


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def atomic_save(path: Path, data):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    temp.replace(path)


def fetch(url: str, sec=False, timeout=23, data=None):
    # Explicit SEC contact is required by SEC access policy; configure via Actions secret.
    ua = os.environ.get("SEC_USER_AGENT", "").strip() if sec else USER_AGENT
    if not ua:
        raise RuntimeError("SEC_USER_AGENT missing: configure the Actions secret")
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": ua,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.7",
        "Accept-Encoding": "identity",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(7_000_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise
        time.sleep(1 + attempt * 2)
    raise RuntimeError("Fetch retries exhausted")


def normal(text: str):
    return re.sub(r"\s+", " ", BeautifulSoup(text or "", "html.parser").get_text(" ")).strip()


def matcher(phrases):
    terms = [re.escape(t).replace(r"\ ", r"\s+") for t in phrases if t.strip()]
    return re.compile(r"(?<![\w])(?:" + "|".join(sorted(terms, key=len, reverse=True)) + r")(?![\w])", re.I) if terms else re.compile(r"(?!)")


def canonical_url(url: str):
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"), urllib.parse.urlencode(q), ""))


def item_key(entry):
    link = entry.get("link", "")
    # The Board's actual IDs allow the same filing/decision discovered from
    # both Docket Yard and the official STB pages to collapse to one alert.
    m = re.search(r"/(filing|decision)/(\d{4,})", link, re.I)
    if not m and entry.get("stb_kind") and entry.get("stb_id"):
        return f"stb:{entry['stb_kind']}:{entry['stb_id']}"
    if m:
        return f"stb:{m.group(1).lower()}:{m.group(2)}"
    seed = canonical_url(link) or entry.get("title", "") + "|" + entry.get("published", "")
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def rss_items(payload, source):
    """Parse RSS 2 / RDF RSS / Atom without assuming one feed vendor's layout."""
    root = ET.fromstring(payload)
    entries = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "link":
                # Atom uses href; RSS usually has link text.
                val = child.attrib.get("href", "") or (child.text or "")
                if val and (child.attrib.get("rel", "alternate") == "alternate" or "link" not in fields):
                    fields[name] = val
            elif name not in fields:
                fields[name] = "".join(child.itertext())
        entry = {
            "title": normal(fields.get("title", "")),
            "link": (fields.get("link", "") or fields.get("guid", "") or fields.get("id", "")).strip(),
            "summary": normal(fields.get("description", "") or fields.get("summary", "") or fields.get("content", ""))[:1200],
            "published": fields.get("pubDate", "") or fields.get("published", "") or fields.get("updated", ""),
            "source": source,
            "community": any(p in source.lower() for p in ("reddit.com", "truckingboards.com")),
        }
        if entry["title"] or entry["link"]:
            entries.append(entry)
    if not entries:
        raise ValueError("No RSS/Atom entries found (check feed validity and endpoint)")
    return entries


def businesswire_items(payload, source):
    """Read public release cards after retirement of the supplied RSS channel."""
    soup = BeautifulSoup(payload, "html.parser")
    entries = []
    seen_links = set()
    for anchor in soup.select('a[href*="/news/home/"]'):
        heading = anchor.find(["h2", "h3"])
        if not heading:
            continue
        link = urllib.parse.urljoin(source, anchor["href"])
        if link in seen_links:
            continue
        seen_links.add(link)
        summary = anchor.parent.select_one(".rich-text")
        entries.append({"title": normal(heading.get_text(" ")),
                        "link": link,
                        "summary": normal(summary.get_text(" "))[:1200] if summary else "",
                        "published": "", "source": source, "community": False})
    if not entries:
        raise ValueError("Business Wire page has no release cards (check page layout/access)")
    return entries


def load_official_stb(url):
    """Load the same public table endpoint used by STB's site (latest 500 records)."""
    payload = fetch(url)
    soup = BeautifulSoup(payload, "html.parser")
    table = soup.select_one("table.stb-ajax-table")
    if table:
        params = {"action": table.get("data-stb-action", ""),
                  "_ajax_nonce": table.get("data-stb-nonce", ""),
                  "page": 1, "per-page": 500, "sort_by": "sortDate", "sort_order": "desc"}
        if not params["action"] or not params["_ajax_nonce"]:
            raise ValueError("STB table endpoint metadata missing")
        endpoint = urllib.parse.urljoin(url, "/wp-admin/admin-ajax.php")
        response = json.loads(fetch(endpoint, data=urllib.parse.urlencode(params).encode()))
        data = response.get("data", {})
        if not response.get("success") or not isinstance(data, dict) or not data.get("rows"):
            raise ValueError("STB dynamic table endpoint returned no rows")
        table.find("tbody").replace_with(BeautifulSoup("<tbody>" + data["rows"] + "</tbody>", "html.parser"))
        payload = str(soup)
    return official_stb_items(payload, url)


def official_stb_items(payload, source):
    soup = BeautifulSoup(payload, "html.parser")
    kind = "decision" if "/decisions/" in source else "comment" if "environmental-comments" in source else "filing"
    entries = []
    headers = [normal(th.get_text(" ")).lower() for th in soup.select("thead th")]
    id_heading = "decision id" if kind == "decision" else "comment id" if "environmental-comments" in source else "filing id"
    for tr in soup.select("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        row_text = normal(tr.get_text(" "))
        if not re.search(r"\bFD[_\s-]*36873(?!\d)", row_text, re.I):
            continue
        attach = tr.find("a", href=re.compile(r"\.pdf(?:\?|$)", re.I))
        # Filing ID is column 3; decisions/comments use column 2. Never
        # scan the docket column, whose 36873 can look like a record ID.
        id_column = headers.index(id_heading) if id_heading in headers else (2 if "/filings/" in source else 1)
        ident = None
        if len(cells) > id_column:
            match = re.fullmatch(r"\s*(\d{5,7})\s*", cells[id_column].get_text(" "))
            if match:
                ident = match.group(1)
        if not ident and attach:
            match = re.search(r"/(\d{5,7})\.pdf(?:\?|$)", attach.get("href", ""), re.I)
            if match:
                ident = match.group(1)
        if not ident:
            continue
        link = urllib.parse.urljoin(source, attach.get("href", "")) if attach else source
        # Include filer / decision summary as context; never invent an interpretation.
        date_column = next((i for i, h in enumerate(headers) if "date" in h), 0)
        date = normal(cells[date_column].get_text(" "))
        title = f"STB FD 36873 {kind} {ident}: " + row_text
        entries.append({"title": title[:220], "summary": row_text[:900], "link": link,
                        "published": date, "source": source, "stb_kind": kind, "stb_id": ident})
    if not soup.find("table"):
        raise ValueError("Official STB page has no table (page/layout may have changed)")
    if not any(normal(td.get_text(" ")) for td in soup.select("table td")):
        raise ValueError("Official STB table has no loaded records; dynamic data endpoint required")
    return entries


def label_item(item, config):
    searchable = " ".join((item.get("title", ""), item.get("summary", ""), item.get("link", "")))
    if matcher(config["exclude_terms"]).search(searchable):
        return None
    if item.get("stb_kind"):
        level = "P1" if item["stb_kind"] == "decision" else "P2"
        return (level, "STB FD 36873", "Official STB")
    if "docketyard.org/d/FD-36873/feed" in item.get("source", ""):
        # A specific docket feed, never merely a search result.
        is_decision = "/decision/" in item.get("link", "")
        return ("P1" if is_decision else "P2", "STB FD 36873", "Docket Yard (verify at STB)")

    company = matcher(config["companies"]).search(searchable)
    urgent = matcher(config["priority_keywords"]).search(searchable)
    event = matcher(config["material_event_keywords"]).search(searchable)
    freight = matcher(config.get("freight_context_keywords", [])).search(searchable)
    is_sec = "sec.gov" in item.get("source", "")
    community = item.get("community", False)
    # Short/ambiguous names should not fire on, e.g., Amazon entertainment news.
    if company and company.group(0).casefold() in {x.casefold() for x in config.get("ambiguous_company_names", [])}:
        if not freight and not is_sec:
            return None
    if community:
        # Social/forum sources are tips, not confirmed facts. Require an event signal.
        if company and urgent and freight:
            return ("TIP", company.group(0), "UNVERIFIED community report")
        return None
    if is_sec:
        return ("P2", company.group(0), "SEC filing — review contents") if company else None
    if company and urgent:
        return ("P1", company.group(0), "Source document — verify")
    if company and event:
        return ("P2", company.group(0), "Source document — verify")
    # Government rules can affect the market without naming a watched company.
    is_reg = "federalregister.gov" in item.get("source", "")
    if is_reg and (urgent or event):
        return ("P2", "Federal Register", "Government notice")
    return None


def publish(title, message, link, priority=3, attempts=3):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("NTFY_TOPIC secret is not set")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", topic):
        raise RuntimeError("NTFY_TOPIC contains invalid characters")
    if link and urllib.parse.urlsplit(link).scheme not in ("https", "http"):
        link = ""
    headers = {
        "Title": title[:110], "Priority": str(priority),
        "Tags": "warning" if priority >= 4 else "memo", "Content-Type": "text/plain; charset=utf-8"
    }
    if link:
        headers["Click"] = link
    request = urllib.request.Request("https://ntfy.sh/" + topic,
                                      data=message[:3800].encode("utf-8"), headers=headers, method="POST")
    for i in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"ntfy HTTP {response.status}")
                return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if i == attempts - 1:
                raise RuntimeError(f"ntfy delivery failed after {attempts} attempts: {exc}") from exc
            time.sleep(i + 1)


def log_delivery(entry, label):
    event = {"at_utc": now(), "priority": label[0], "tag": label[1],
             "title": entry["title"], "link": entry.get("link", ""),
             "source": entry["source"], "note": label[2]}
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def expanded_sources(cfg):
    sources = [("docket:" + cfg["stb_docket_feed"], cfg["stb_docket_feed"], False)]
    for url in cfg["rss_feeds"]:
        if "sec.gov" in url:
            # Three overlapping pages reduce but cannot eliminate EDGAR coverage gaps.
            for start in (0, 100, 200):
                parsed = urllib.parse.urlsplit(url)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                query["start"] = str(start)
                sources.append((f"sec:{start}", urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                parsed.path, urllib.parse.urlencode(query), parsed.fragment)), False))
        else:
            sources.append((url, url, False))
    for url in cfg.get("html_feeds", []):
        sources.append((url, url, False))
    for url in cfg["stb_official_pages"]:
        sources.append(("official:" + url, url, True))
    return sources


def collect_source(source):
    source_id, url, is_html = source
    try:
        if is_html:
            entries = load_official_stb(url)
        elif "businesswire.com/newsroom/" in url:
            entries = businesswire_items(fetch(url), url)
        else:
            entries = rss_items(fetch(url, sec=source_id.startswith("sec:")), url)
        return entries, None
    except Exception as exc:
        return None, exc


def run(cfg, dry_run=False):
    state = load_json(STATE_FILE, {"version": 2, "seen": {}, "sources": {}})
    if state.get("version") != 2:
        raise ValueError("Unsupported seen_items.json version; do not silently discard history")
    seen, sources = state["seen"], state["sources"]
    source_count = 0
    new_count = 0
    errors = []
    queue = []
    total = 0
    source_specs = expanded_sources(cfg)
    # Bound concurrency so a few slow endpoints cannot exhaust the Actions timeout.
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(collect_source, source_specs))
    for (source_id, url, is_html), (entries, error) in zip(source_specs, fetched):
        health = sources.setdefault(source_id, {"seeded": False, "failures": 0})
        try:
            if error:
                raise error
            if not entries and not is_html:
                raise ValueError("No parseable entries (feed may have been retired or blocked)")
            source_count += 1
            health.update({"last_success": now(), "last_count": len(entries), "failures": 0, "error": ""})
            for item in entries:
                key = item_key(item)
                if key in seen:
                    continue
                total += 1
                label = label_item(item, cfg)
                record = {"first_seen": now(), "title": item["title"][:180],
                          "status": "pending" if label else "ignored", "entry": item if label else None,
                          "label": label}
                seen[key] = record
                if health["seeded"] and label:
                    queue.append((key, record))
                    new_count += 1
                else:
                    # On a source's first successful read, seed existing entries quietly.
                    record["status"] = "baseline" if not health["seeded"] else "ignored"
                    record["entry"] = None
                    record["label"] = None
            if not health["seeded"]:
                health["seeded"] = True
                LOG.info("Baseline seeded for %s (%s entries)", source_id, len(entries))
        except Exception as exc:
            health["failures"] = health.get("failures", 0) + 1
            health["error"] = str(exc)[:260]
            health["last_error"] = now()
            errors.append((source_id, str(exc)))
            LOG.warning("Source failed: %s: %s", source_id, exc)
    # Recover notification attempts from previous runs. Failures stay pending.
    queued_keys = {key for key, _ in queue}
    for key, record in seen.items():
        if record.get("status") == "pending" and key not in queued_keys:
            queue.append((key, record))
    queue.sort(key=lambda p: (0 if p[1]["label"][0] == "P1" else 1, p[1]["first_seen"]))
    delivered = 0
    # Batch routine docket filings to avoid turning heavy filing days into 100+ pushes.
    docket = [(k, r) for k, r in queue if r["label"][0] == "P2" and r["label"][1] == "STB FD 36873"]
    others = [(k, r) for k, r in queue if (k, r) not in docket]
    if docket:
        batch = docket[:30]
        summary = "\n".join("• " + r["entry"]["title"][:110] for _, r in batch[:6])
        if len(batch) > 6:
            summary += f"\n...plus {len(batch)-6} more filings."
        msg = f"{len(batch)} new records in FD 36873 (filings/comments):\n{summary}\n\nReview each source link in alerts.jsonl. Verify against STB."
        link = cfg["stb_docket_feed"]
        if dry_run:
            LOG.info("[DRY RUN] docket batch: %s", msg)
        else:
            try:
                publish("STB FD 36873: new records", msg, link, priority=3)
                for key, r in batch:
                    log_delivery(r["entry"], r["label"])
                    r["status"] = "sent"
                    r["entry"] = None
                delivered += 1
            except RuntimeError as exc:
                errors.append(("ntfy", str(exc)))
                LOG.error("STB notification failure (will retry): %s", exc)
    for key, record in others[:MAX_ALERTS_PER_RUN]:
        entry, label = record["entry"], record["label"]
        title = f"{label[0]} | {label[1]}"
        message = f"{entry['title']}\nPublished: {entry.get('published') or 'Not provided'}\nSource: {label[2]}\nOriginal: {entry.get('link', '')}"
        if dry_run:
            LOG.info("[DRY RUN] %s: %s", title, message)
            continue
        try:
            publish(title, message, entry.get("link", ""), priority=4 if label[0] == "P1" else 3)
            log_delivery(entry, label)
            record["status"] = "sent"
            record["entry"] = None
            delivered += 1
        except RuntimeError as exc:
            errors.append(("ntfy", str(exc)))
            LOG.error("Alert failed (will retry): %s", exc)
            break  # don't overwhelm a failing notification endpoint

    # Health check: one consolidated alert every 12h when any source fails 3+ times.
    active_sources = {sid: sources[sid] for sid, _, _ in source_specs}
    failing = [(sid, h) for sid, h in active_sources.items() if h.get("failures", 0) >= 3]
    if failing and not dry_run:
        last = state.get("last_health_alert_epoch", 0)
        if time.time() - last >= 43200:
            msg = "Some sources failed repeatedly; coverage may be incomplete:\n" + "\n".join(
                f"• {sid[:75]}: {h['error'][:95]}" for sid, h in failing[:8])
            try:
                publish(f"Tripwire health: {len(failing)} sources failing", msg,
                        "https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "metshr31/freight-tripwire") + "/actions", priority=4)
                state["last_health_alert_epoch"] = time.time()
            except RuntimeError as exc:
                errors.append(("health push", str(exc)))

    if len(seen) > MAX_SEEN:
        # Preserve every undelivered alert; prune only old processed records.
        eligible = sorted((k for k, r in seen.items() if r.get("status") != "pending"),
                          key=lambda k: seen[k]["first_seen"])
        for k in eligible[:max(0, len(seen) - MAX_SEEN)]:
            del seen[k]
    state["last_run_utc"] = now()
    health_doc = {"last_run_utc": now(), "successful_sources": source_count,
                  "source_count": len(active_sources), "queued_alerts": sum(r.get("status") == "pending" for r in seen.values()),
                  "notification_batches_sent": delivered, "source_errors": len(errors), "sources": active_sources}
    if not dry_run:
        atomic_save(STATE_FILE, state)
        atomic_save(HEALTH_FILE, health_doc)
    LOG.info("Completed: %s sources OK; %s new qualifying items; %s pushes; %s pending; %s errors",
             source_count, new_count, delivered, health_doc["queued_alerts"], len(errors))
    # Fail visibly if *all* sources failed. Partial failures are recorded in health.json.
    return 1 if source_count == 0 else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-push", action="store_true", help="Publish a real delivery test to your phone")
    parser.add_argument("--check-config", action="store_true", help="Validate config, no network")
    parser.add_argument("--dry-run", action="store_true", help="Read sources but send/save nothing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_json(WATCHLIST_FILE, None)
    if not isinstance(cfg, dict) or not cfg.get("companies") or not cfg.get("rss_feeds"):
        parser.error("watchlist.json is missing or incomplete")
    if args.check_config:
        print(json.dumps({"company_alias_entries": len(cfg["companies"]),
                          "rss_feeds": len(cfg["rss_feeds"]),
                          "html_feeds": len(cfg.get("html_feeds", [])),
                          "stb_pages": len(cfg["stb_official_pages"]),
                          "docket_feed": cfg["stb_docket_feed"]}, indent=2))
        return 0
    if args.test_push:
        publish("Freight Tripwire test", "Freight Tripwire delivery test accepted by ntfy. This is a test, not a news alert. Check your phone to confirm device delivery.",
                "https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "metshr31/freight-tripwire") + "/actions", priority=3)
        print("ntfy accepted the test push. Confirm it appeared on the phone.")
        return 0
    return run(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
