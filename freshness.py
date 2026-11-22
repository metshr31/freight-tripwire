"""Strict publication-time checks. Discovery time is never publication time."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from urllib.parse import urlsplit, urljoin

from bs4 import BeautifulSoup


def timestamp(value):
    value = str(value or "").strip()
    # A calendar date has no trustworthy hour. Naive times are also ambiguous.
    if not re.search(r"\d{1,2}:\d{2}", value):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(timezone.utc)


def verdict(item, minutes=120, at=None):
    if minutes == 0:  # Explicit opt-out used by unrelated isolated unit fixtures.
        return "fresh"
    published = timestamp(item.get("published"))
    if published is None:
        return "unverified_publication_time"
    age = ((at or datetime.now(timezone.utc)) - published).total_seconds()
    if age < -300:
        return "future_publication_time"
    return "fresh" if age <= minutes * 60 else "stale"


def publication_from_page(payload, target_url=None):
    """Use explicit publisher timestamps, never footer dates or dateModified."""
    soup = BeautifulSoup(payload, "html.parser")
    for selector in ['meta[property="article:published_time"]', 'meta[name="datePublished"]',
                     'meta[itemprop="datePublished"]', 'time[itemprop="datePublished"][datetime]']:
        tag = soup.select_one(selector)
        if tag:
            value = tag.get("content") or tag.get("datetime")
            if timestamp(value):
                return value
    def same_document(value):
        if not target_url or not value:
            return True
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        if not isinstance(value, str):
            return False
        return urljoin(target_url, value).split("#")[0].rstrip("/") == target_url.split("#")[0].rstrip("/")
    def find(node):
        if isinstance(node, dict):
            # Only dated editorial documents, not an Organization's founding date.
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if any(t in {"Article", "NewsArticle", "BlogPosting", "Report", "TechArticle"} for t in types):
                if not same_document(node.get("url") or node.get("mainEntityOfPage") or node.get("@id")):
                    return None
                value = node.get("datePublished")
                if timestamp(value):
                    return value
            for key, value in node.items():
                if key in {"relatedLink", "isRelatedTo", "mentions", "itemListElement", "hasPart"}:
                    continue
                match = find(value)
                if match:
                    return match
        elif isinstance(node, list):
            for value in node:
                match = find(value)
                if match:
                    return match
        return None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = find(json.loads(script.string or script.get_text()))
            if value:
                return value
        except (ValueError, TypeError):
            continue
    return None


def resolve(item, fetch, allowed_hosts):
    url = item.get("link", "")
    # A homepage's own publication date does not date each inline notice on it.
    if url.rstrip("/") == item.get("source", "").rstrip("/"):
        return False
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if host not in allowed_hosts or urlsplit(url).path.lower().endswith((".pdf", ".zip")):
        return False
    try:
        value = publication_from_page(fetch(url, timeout=4, attempts=1), url)
    except Exception:
        return False
    if value:
        item["published"] = value
        item["publication_time_basis"] = "publisher metadata on original page"
        return True
    return False
