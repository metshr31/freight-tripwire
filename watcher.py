#!/usr/bin/env python3
"""Freight Tripwire: lightweight, auditable freight leads (not a verification engine)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import Header
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
from primary import html_items as primary_html_items, decorate as decorate_primary, inline_items, document_items, central_items, notice_items, lazaro_items

from freshness import verdict as freshness_verdict, timestamp as publication_timestamp, resolve as resolve_publication_time

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "seen_items.json"
HEALTH_FILE = BASE / "health.json"
LOG_FILE = BASE / "alerts.jsonl"
WATCHLIST_FILE = BASE / "watchlist.json"
MAX_SEEN = 100000
MAX_ALERTS_PER_RUN = 20
LOG = logging.getLogger("freight-tripwire")
UTC = timezone.utc
USER_AGENT = "FreightTripwire/1.1 (news monitoring; public sources)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,application/atom+xml;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
}


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


def _fetch_curl_cffi(url: str, timeout=23, data=None, headers=None):
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    kwargs = {
        "impersonate": "chrome",
        "timeout": timeout,
        "headers": headers or {},
    }
    if data is not None:
        resp = cffi_requests.post(url, data=data, **kwargs)
    else:
        resp = cffi_requests.get(url, **kwargs)
    if resp.status_code >= 400:
        raise urllib.error.HTTPError(url, resp.status_code, f"HTTP {resp.status_code}", resp.headers, None)
    return resp.text[:7_000_000]


def fetch(url: str, sec=False, timeout=23, data=None, attempts=3, request_headers=None):
    # Explicit SEC contact is required by SEC access policy; configure via Actions secret.
    if sec:
        ua = os.environ.get("SEC_USER_AGENT", "").strip()
        if not ua:
            raise RuntimeError("SEC_USER_AGENT missing: configure the Actions secret")
        base_headers = {
            "User-Agent": ua,
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.7",
            "Cache-Control": "no-cache",
        }
    else:
        base_headers = dict(BROWSER_HEADERS)
    cffi_headers = {**base_headers, **(request_headers or {})}
    urllib_headers = {**cffi_headers, "Accept-Encoding": "identity"}
    request = urllib.request.Request(url, data=data, headers=urllib_headers)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(7_000_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if not sec and exc.code in (403, 429):
                try:
                    cffi_body = _fetch_curl_cffi(url, timeout=timeout, data=data, headers=cffi_headers)
                    if cffi_body is not None:
                        return cffi_body
                except urllib.error.HTTPError as cffi_exc:
                    exc = cffi_exc
                except Exception:
                    pass
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise exc
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts - 1:
                raise
        time.sleep(min(8, 2 ** attempt))
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
    seed = entry.get("primary_item_id") or canonical_url(link) or entry.get("title", "") + "|" + entry.get("published", "")
    if entry.get("revision"):
        seed += "|revision:" + entry["revision"]
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
            "published": fields.get("pubDate", "") or fields.get("published", ""),
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
    if item.get("primary_company"):
        if item.get("require_freight_context") and not freight:
            return None
        name = item["primary_company"]
        source_label = item.get("primary_source_label", "Official company source")
        headline = item.get("title", "")
        service = item.get("primary_role") == "service"
        if not service and re.search(r"^(?:how to\b|a guide to\b|the guide to\b|fleet recall campaigns:)", headline, re.I):
            return None
        headline_urgent = matcher(config["priority_keywords"]).search(headline)
        # News cards can contain unrelated neighboring text. Require the event
        # in the headline; service notices retain their body-level signals.
        if headline_urgent or (service and urgent):
            return ("P1", name, source_label)
        if service or matcher(config["material_event_keywords"]).search(headline):
            return ("P2", name, source_label)
        return None
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
        "Title": title[:110] if title[:110].isascii() else Header(title[:110], "utf-8").encode(), "Priority": str(priority),
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
             "source": entry["source"], "note": label[2],
             "published": entry.get("published", ""), "excerpt": entry.get("summary", "")[:500],
             "primary_source_id": entry.get("primary_source_id")}
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
    if cfg.get("primary_sources_file"):
        registry = load_json(BASE / cfg["primary_sources_file"], None)
        if not isinstance(registry, dict) or not registry.get("sources"):
            raise ValueError("Primary source registry is missing or empty")
        for spec in registry["sources"]:
            if spec.get("enabled", True):
                sources.append(("primary:" + spec["id"], spec["url"], spec))
    if len({s[0] for s in sources}) != len(sources):
        raise ValueError("Source IDs must be unique")
    def priority(source):
        sid, _, spec = source
        if isinstance(spec, dict):
            return 0 if spec["role"] == "service" else 1
        if sid.startswith(("docket:", "official:")):
            return 2
        return 4 if any(host in sid for host in ("reddit.com", "truckingboards.com")) else 3
    return sorted(sources, key=priority)


def collect_source(source):
    source_id, url, is_html = source
    try:
        if isinstance(is_html, dict):
            spec = is_html
            body = json.dumps(spec["post_json"]).encode() if spec.get("post_json") is not None else None
            raw = fetch(spec.get("request_url", url),
                        timeout=spec.get("timeout", 20),
                        attempts=spec.get("attempts", 2),
                        data=body,
                        request_headers={"Content-Type": "application/json"} if body else None)
            parsers = {"rss": lambda raw, spec: rss_items(raw, spec["url"]), "html": primary_html_items,
                       "inline": inline_items, "documents": document_items, "central": central_items, "notices": notice_items, "lazaro": lazaro_items}
            items = parsers[spec["kind"]](raw, spec)
            entries = decorate_primary(items, spec)
        elif is_html:
            entries = load_official_stb(url)
        elif "businesswire.com/newsroom/" in url:
            entries = businesswire_items(fetch(url), url)
        else:
            entries = rss_items(fetch(url, sec=source_id.startswith("sec:")), url)
        return entries, None
    except Exception as exc:
        return None, exc


def collect_completed(source_specs):
    """Yield the fastest finished source first, independent of slow social feeds."""
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(collect_source, spec): spec for spec in source_specs}
        for future in as_completed(futures):
            entries, error = future.result()
            yield futures[future], (entries, error)


def deliver_record(record):
    entry, label = record["entry"], record["label"]
    message = f"{entry['title']}\nPublished: {entry.get('published') or 'Not provided'}\nSource: {label[2]}\nOriginal: {entry.get('link', '')}"
    if entry.get("summary"):
        message += "\nSource excerpt: " + entry["summary"][:500]
    publish(f"{label[0]} | {label[1]}", message, entry.get("link", ""),
            priority=4 if label[0] == "P1" else 3)
    log_delivery(entry, label)
    record["status"] = "sent"
    record["entry"] = None


def run(cfg, dry_run=False):
    started = time.monotonic()
    state = load_json(STATE_FILE, {"version": 2, "seen": {}, "sources": {}})
    if state.get("version") != 2:
        raise ValueError("Unsupported seen_items.json version; do not silently discard history")
    seen, sources = state["seen"], state["sources"]
    source_count = 0
    new_count = 0
    errors = []
    queue = []
    total = 0
    freshness_holds = {}
    timestamp_lookups = 0
    max_age = cfg.get("max_alert_age_minutes", 120)
    source_specs = expanded_sources(cfg)
    if not os.environ.get("SEC_USER_AGENT", "").strip():
        LOG.info("SEC_USER_AGENT not configured; skipping sec:* EDGAR checks")
        source_specs = [s for s in source_specs if not s[0].startswith("sec:")]
    for sid, url, spec in source_specs:
        sources.setdefault(sid, {"seeded": False, "failures": 0})
    due_specs = [s for s in source_specs if sources[s[0]].get("next_check_epoch", 0) <= time.time()]
    delivered = 0
    push_failed = False
    for (source_id, url, is_html), (entries, error) in collect_completed(due_specs):
        health = sources.setdefault(source_id, {"seeded": False, "failures": 0})
        try:
            if error:
                raise error
            if not entries and not is_html:
                raise ValueError("No parseable entries (feed may have been retired or blocked)")
            source_count += 1
            health.update({"last_success": now(), "last_count": len(entries), "failures": 0, "error": "", "next_check_epoch": 0})
            if isinstance(is_html, dict):
                health.update({"company": is_html["company"], "role": is_html["role"], "url": url, "kind": is_html["kind"],
                               "country": is_html.get("country"), "modes": is_html.get("modes", [])})
            for item in entries:
                key = item_key(item)
                if key in seen:
                    continue
                total += 1
                label = label_item(item, cfg)
                hold_reason = None
                if health["seeded"] and label:
                    reason = freshness_verdict(item, max_age)
                    if reason == "unverified_publication_time" and item.get("primary_company") and timestamp_lookups < 10:
                        timestamp_lookups += 1
                        hosts = {urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")}
                        if isinstance(is_html, dict):
                            hosts.update(is_html.get("allowed_hosts", []))
                        resolve_publication_time(item, fetch, hosts)
                        reason = freshness_verdict(item, max_age)
                    if reason != "fresh":
                        hold_reason = reason
                        freshness_holds[reason] = freshness_holds.get(reason, 0) + 1
                        label = None
                record = {"first_seen": now(), "title": item["title"][:180],
                          "status": "pending" if label else "ignored", "entry": item if label else None,
                          "label": label}
                if hold_reason:
                    record["freshness_hold"] = hold_reason
                seen[key] = record
                if health["seeded"] and label:
                    queue.append((key, record))
                    new_count += 1
                    # Notify on a direct company update as soon as it is parsed.
                    send_now = delivered < (MAX_ALERTS_PER_RUN if label[0] == "P1" else MAX_ALERTS_PER_RUN // 2)
                    if item.get("primary_company") and not dry_run and not push_failed and send_now:
                        try:
                            deliver_record(record)
                            delivered += 1
                            atomic_save(STATE_FILE, state)
                        except RuntimeError as exc:
                            errors.append(("ntfy", str(exc)))
                            push_failed = True
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
            # Public access restrictions are reported, never retried every five minutes.
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
                health["next_check_epoch"] = time.time() + 1800
            elif isinstance(is_html, dict) and health["failures"] >= 3:
                health["next_check_epoch"] = time.time() + 900
            errors.append((source_id, str(exc)))
            LOG.warning("Source failed: %s: %s", source_id, exc)
    # Recover notification attempts from previous runs. Failures stay pending.
    queued_keys = {key for key, _ in queue}
    for key, record in seen.items():
        if record.get("status") == "pending" and key not in queued_keys:
            queue.append((key, record))
    # A previously queued alert must still be fresh at delivery time.
    for _, record in queue:
        if record.get("status") == "pending":
            reason = freshness_verdict(record["entry"], max_age)
            if reason != "fresh":
                record.update(status="expired", entry=None, freshness_hold=reason)
                freshness_holds[reason] = freshness_holds.get(reason, 0) + 1
    queue = [(key, record) for key, record in queue if record.get("status") == "pending"]
    queue.sort(key=lambda p: (0 if p[1]["label"][0] == "P1" else 1, p[1]["first_seen"]))
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
    for key, record in others[:max(0, MAX_ALERTS_PER_RUN - delivered)]:
        entry, label = record["entry"], record["label"]
        title = f"{label[0]} | {label[1]}"
        message = f"{entry['title']}\nPublished: {entry.get('published') or 'Not provided'}\nSource: {label[2]}\nOriginal: {entry.get('link', '')}"
        if dry_run:
            LOG.info("[DRY RUN] %s: %s", title, message)
            continue
        try:
            deliver_record(record)
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
    covered_names = set()
    for sid, _, spec in source_specs:
        h = sources[sid]
        if isinstance(spec, dict) and h.get("seeded") and h.get("failures", 0) == 0:
            covered_names.update(spec.get("aliases", [spec["company"]]))
    regional = {}
    for sid, _, spec in source_specs:
        if not isinstance(spec, dict) or not (spec.get("country") or spec.get("countries")):
            continue
        operational = sources[sid].get("seeded") and sources[sid].get("failures", 0) == 0
        for country_code in spec.get("countries", [spec.get("country")]):
            country = regional.setdefault(country_code, {})
            for mode in spec.get("modes", []):
                bucket = country.setdefault(mode, {"registered": set(), "operational": set()})
                bucket["registered"].add(spec["company"])
                if operational:
                    bucket["operational"].add(spec["company"])
    regional = {country: {mode: {"registered_entities": sorted(b["registered"]),
                                "operational_entities": sorted(b["operational"]),
                                "entities_without_working_reader": sorted(b["registered"] - b["operational"])}
                         for mode, b in modes.items()} for country, modes in regional.items()}
    health_doc = {"last_run_utc": now(), "regional_coverage": regional,
                  "freshness_holds": freshness_holds, "max_alert_age_minutes": max_age, "successful_sources": source_count,
                  "source_count": len(active_sources), "queued_alerts": sum(r.get("status") == "pending" for r in seen.values()),
                  "notification_batches_sent": delivered, "source_errors": len(errors), "sources": active_sources,
                  "checked_sources": len(due_specs), "backoff_sources": len(source_specs) - len(due_specs),
                  "elapsed_seconds": round(time.monotonic() - started, 2),
                  "primary_sources": sum(isinstance(s[2], dict) for s in source_specs),
                  "primary_sources_successful": sum(sid.startswith("primary:") and h.get("failures") == 0 and h.get("seeded") for sid, h in active_sources.items()),
                  "direct_company_names_covered": len(covered_names),
                  "company_names_without_live_primary": sorted(set(cfg["companies"]) - covered_names) if cfg.get("primary_sources_file") else []}
    if not dry_run:
        atomic_save(STATE_FILE, state)
        atomic_save(HEALTH_FILE, health_doc)
    LOG.info("Completed: %s sources OK; %s new qualifying items; %s pushes; %s pending; %s errors",
             source_count, new_count, delivered, health_doc["queued_alerts"], len(errors))
    # Fail visibly if *all* sources failed. Partial failures are recorded in health.json.
    return 1 if source_count == 0 or push_failed else 0


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
                          "primary_sources": sum(isinstance(s[2], dict) for s in expanded_sources(cfg)),
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