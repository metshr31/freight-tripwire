"""Parsers for explicitly registered first-party news and service sources."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def canonical(url):
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(query), ""))


GENERIC = re.compile(r"^(read more|learn more|view more|more|details|continue reading|click here|download|news|newsroom|(?:20\d\d\s+)?press releases|service bulletins|bargaining updates|media|blog|investor relations|next|previous|all news|view all|rss|subscribe|company announcements|news media contacts|corporate fact sheet|corporate profile|contact our press department|optimize supply chain|read safety news|delivery notification)$", re.I)
ARTICLE = re.compile(r"news|press|releases?|articles?|blogs?|alerts?|advisories|announcements?|notifications?|stories|insights?|updates?|bulletins?|/20\d\d/", re.I)
REJECT = re.compile(r"privacy|cookies|terms-of|/tag/|/category/|/author/|/page/\d|javascript:|mailto:|tel:|login|sign-in|subscribe|email-alert|/search", re.I)


def html_items(payload, spec):
    soup = BeautifulSoup(payload, "html.parser")
    page_title = clean(soup.title.get_text(" ")) if soup.title else ""
    if re.search(r"access denied|just a moment|pardon our interruption|verify.*human", page_title, re.I):
        raise ValueError("Source did not provide a readable public page")
    for node in soup.select("script,style,nav,footer,header,[role=navigation]"):
        node.decompose()
    scope = soup.select_one(spec.get("container", "main")) or soup
    selector = spec.get("selector", "a[href]")
    allowed_hosts = {urlsplit(spec["url"]).netloc.lower().removeprefix("www.")}
    allowed_hosts.update(spec.get("allowed_hosts", []))
    matches = re.compile(spec["link_pattern"], re.I) if spec.get("link_pattern") else ARTICLE
    result = {}
    for anchor in scope.select(selector):
        href = anchor.get("href", "")
        if not href or href.startswith("#"):
            continue
        url = urljoin(spec["url"], href)
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or REJECT.search(url):
            continue
        if p.netloc.lower().removeprefix("www.") not in allowed_hosts:
            continue
        headline = anchor.find(["h2", "h3", "h4"]) or anchor.find_parent(["h2", "h3", "h4"])
        if canonical(url) == canonical(spec["url"]):
            continue
        if not matches.search(p.path) and not (spec.get("allow_headline_links") and headline):
            continue
        # An index has no article slug; do not turn navigation into news.
        if re.search(r"/(?:news|newsroom|media|press|press-releases|news-releases|blog|insights|alerts|announcements|customer-notifications)/?$", p.path, re.I):
            continue
        if p.path.lower().endswith("/default.aspx") and "details" not in p.path.lower():
            continue
        title = clean((headline or anchor).get_text(" ", strip=True)).strip(" >›»→…")
        card = anchor.find_parent(["article", "li"]) or anchor.parent
        if GENERIC.fullmatch(title) or len(title) < 15:
            heading = card.find(["h2", "h3", "h4"]) if card else None
            if not heading:
                for ancestor in list(anchor.parents)[:4]:
                    if len(ancestor.get_text(" ", strip=True)) > 3000:
                        break
                    heading = ancestor.find(["h2", "h3", "h4", "h5"])
                    if heading:
                        card = ancestor
                        break
            if not heading:
                continue
            title = clean(heading.get_text(" ", strip=True))
        if not 15 <= len(title) <= 500 or GENERIC.fullmatch(title):
            continue
        summary = ""
        published = ""
        if card:
            paragraphs = card.find_all("p", limit=3) if len(card.get_text(" ", strip=True)) < 4000 else []
            summary = clean(" ".join(x.get_text(" ", strip=True) for x in paragraphs))[:1200]
            date = card.find("time")
            if date:
                published = date.get("datetime") or clean(date.get_text(" "))
        item = {"title": title, "summary": summary, "link": canonical(url),
                "published": published, "source": spec["url"], "community": False}
        key = canonical(url)
        if key not in result or len(title) > len(result[key]["title"]):
            result[key] = item
    if not result:
        raise ValueError("No article/advisory links parsed; source is not verified operational")
    return list(result.values())[:100]


def decorate(items, spec):
    for item in items:
        item.update({"primary_company": spec["company"], "primary_role": spec["role"],
                     "primary_source_id": spec["id"], "require_freight_context": spec.get("require_freight_context", False)})
        if spec.get("track_updates"):
            # Updates at an unchanged advisory URL must remain detectable.
            # Use exact headline/summary, never a relative-time label or fetch timestamp.
            item["revision"] = hashlib.sha256((item["title"] + "|" + item.get("summary", "")).encode()).hexdigest()[:16]
    return items


def inline_items(payload, spec):
    """Southeastern publishes releases inline with stable, owned fragment IDs."""
    soup = BeautifulSoup(payload, "html.parser")
    result = []
    for heading in soup.select(spec["selector"]):
        title = clean(heading.get_text(" "))
        if len(title) < 15 or not heading.get("id"):
            continue
        paragraphs = []
        for sibling in heading.find_next_siblings():
            if sibling.name == heading.name and sibling.get("id"):
                break
            if sibling.name == "p":
                paragraphs.append(clean(sibling.get_text(" ")))
            if len(paragraphs) == 3:
                break
        link = spec["url"].split("#")[0] + "#" + heading["id"]
        result.append({"title": title, "summary": " ".join(paragraphs)[:1200],
                       "link": link, "source": spec["url"], "published": "",
                       "primary_item_id": link})
    if not result:
        raise ValueError("Inline release headings not found")
    return result[:100]


def document_items(payload, spec):
    """Read titled release links, including Heartland's paragraph/PDF layout."""
    soup = BeautifulSoup(payload, "html.parser")
    result = []
    for anchor in soup.select('a[href^="/s/"]'):
        paragraph = anchor.find_parent("p")
        if not paragraph:
            continue
        heading = None
        for sibling in paragraph.find_previous_siblings("p", limit=3):
            heading = sibling.find("strong")
            if heading:
                break
        if not heading:
            continue
        title = clean(heading.get_text(" "))
        if len(title) < 15:
            continue
        result.append({"title": title, "summary": clean(sibling.get_text(" "))[:1200],
                       "link": urljoin(spec["url"], anchor["href"]),
                       "source": spec["url"], "published": ""})
    if not result:
        raise ValueError("No titled press release documents parsed")
    return result[:100]


def central_items(payload, spec):
    """Public JSON news service advertised by Central Transport's own news page."""
    data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("d"), list):
        raise ValueError("Central Transport news API schema changed")
    result = []
    for row in data["d"]:
        if not row.get("ArticleTitle") or row.get("ItemId") is None:
            raise ValueError("Central Transport news record lacks title or ID")
        date = row.get("DateToBePublished", "")
        match = re.search(r"/Date\((\d+)", str(date))
        if match:
            date = datetime.fromtimestamp(int(match.group(1)) / 1000, timezone.utc).isoformat()
        result.append({"title": clean(row["ArticleTitle"]),
                       "summary": clean(BeautifulSoup(row.get("ArticleHtml") or "", "html.parser").get_text(" "))[:1200],
                       "published": str(date), "source": spec["url"],
                       "link": urljoin(spec["url"], "news?id=" + str(row["ItemId"]))})
    return result[:100]


