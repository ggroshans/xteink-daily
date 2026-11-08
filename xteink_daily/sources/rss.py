"""RSS 2.0 / RSS 1.0 (RDF) / Atom feeds -> sections of articles."""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.entities import name2codepoint
from typing import Any, Dict, List, Optional, Tuple

from ..api import Article, Section, Source, SourceContext
from ..html_clean import clean_html, inline_text, word_count
from ..net import http_get

MAX_FEED_BYTES = 8 * 1024 * 1024

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "rss1": "http://purl.org/rss/1.0/",
}
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?)?\s*(Z|[+-]\d\d:?\d\d)?\s*$")


@dataclass
class FeedItem:
    uid: str
    title: str
    link: str
    author: str
    published: Optional[dt.datetime]
    body: str  # HTML or text


def fetch(url: str, timeout: float = 20.0) -> bytes:
    data, _headers = http_get(
        url, accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        max_bytes=MAX_FEED_BYTES, timeout=timeout)
    return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(data: bytes) -> ET.Element:
    # Feeds never need a DOCTYPE; dropping it rules out entity-expansion tricks.
    data = re.sub(rb"<!DOCTYPE[^>\[]*(\[.*?\])?\s*>", b"", data, count=1, flags=re.S | re.I)
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    # Common breakage: HTML entities (&nbsp;) or bare ampersands in an XML feed.
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)", data)
    encoding = m.group(1).decode() if m else "utf-8"
    try:
        text = data.decode(encoding, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)

    def entity(mo: "re.Match[str]") -> str:
        name = mo.group(1)
        if name in _XML_ENTITIES:
            return mo.group(0)
        cp = name2codepoint.get(name)
        return f"&#{cp};" if cp else f"&amp;{name};"

    text = re.sub(r"&([A-Za-z][A-Za-z0-9]*);", entity, text)
    text = re.sub(r"&(?!(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);)", "&amp;", text)
    return ET.fromstring(text.encode("utf-8"))


def parse_date(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    value = value.strip()
    m = _ISO_RE.match(value)
    if m:
        y, mo, d, hh, mm, ss, tz = m.groups()
        tzinfo = dt.timezone.utc
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            tzinfo = dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        try:
            return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0),
                               tzinfo=tzinfo)
        except ValueError:
            return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _atom_content(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    kind = el.get("type", "text")
    if kind == "xhtml":
        children = list(el)
        container = children[0] if len(children) == 1 and _local(children[0].tag) == "div" else el
        for node in container.iter():
            if isinstance(node.tag, str) and "}" in node.tag:
                node.tag = _local(node.tag)
        return (container.text or "") + "".join(
            ET.tostring(c, encoding="unicode") for c in container)
    if kind in ("html", "text/html"):
        return el.text or ""
    # Plain text: escape so the cleaner treats it as text, not markup.
    return (el.text or "").replace("&", "&amp;").replace("<", "&lt;")


def _atom_title(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    if el.get("type") == "xhtml":
        return inline_text(_atom_content(el))
    # Titles are often double-escaped HTML regardless of declared type.
    return inline_text(el.text or "")


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for link in entry.findall("atom:link", NS):
        rel = link.get("rel", "alternate")
        if rel == "alternate" and link.get("type", "text/html").startswith("text/html"):
            return link.get("href", "")
        if not fallback and rel == "alternate":
            fallback = link.get("href", "")
    return fallback


def parse(data: bytes) -> Tuple[str, List[FeedItem]]:
    root = _parse_xml(data)
    kind = _local(root.tag)
    items: List[FeedItem] = []

    if kind == "feed":
        feed_title = _atom_title(root.find("atom:title", NS))
        for e in root.findall("atom:entry", NS):
            body = _atom_content(e.find("atom:content", NS)) or _atom_content(e.find("atom:summary", NS))
            items.append(FeedItem(
                uid=_text(e.find("atom:id", NS)),
                title=_atom_title(e.find("atom:title", NS)),
                link=_atom_link(e),
                author=_text(e.find("atom:author/atom:name", NS)),
                published=parse_date(_text(e.find("atom:published", NS)) or _text(e.find("atom:updated", NS))),
                body=body,
            ))
        return feed_title, items

    if kind == "rss":
        channel = root.find("channel")
        if channel is None:
            return "", []
        raw_items = channel.findall("item")
        feed_title = inline_text(_text(channel.find("title")))
    elif kind == "RDF":
        raw_items = root.findall("rss1:item", NS)
        feed_title = inline_text(_text(root.find("rss1:channel/rss1:title", NS)))
    else:
        raise ValueError(f"not a feed (root element <{kind}>)")

    def find(item: ET.Element, name: str) -> str:
        return _text(item.find(name)) or _text(item.find(f"rss1:{name}", NS))

    for it in raw_items:
        body = _text(it.find("content:encoded", NS)) or find(it, "description")
        items.append(FeedItem(
            uid=find(it, "guid") or find(it, "link"),
            title=inline_text(find(it, "title")),
            link=find(it, "link"),
            author=_text(it.find("dc:creator", NS)) or find(it, "author"),
            published=parse_date(find(it, "pubDate") or _text(it.find("dc:date", NS))),
            body=body,
        ))
    return feed_title, items


def item_uid(feed_url: str, item: FeedItem) -> str:
    key = item.uid or item.link or item.title
    return hashlib.sha1(f"{feed_url}\n{key}".encode("utf-8")).hexdigest()[:16]


def _feed_spec(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        return {"url": raw}
    if isinstance(raw, dict) and raw.get("url"):
        return raw
    raise ValueError(f"bad feed entry: {raw!r}")


def _with_full_text(ctx: SourceContext, articles: List[Article]) -> None:
    """Replace summaries with the article page's main text where extraction looks right."""
    targets = [a for a in articles if a.link]
    results = ctx.map(lambda a: ctx.article_html(a.link), targets)
    upgraded = 0
    for art, body in zip(targets, results):
        if body and word_count(body) >= max(120, 1.5 * word_count(art.html)):
            art.html = body
            upgraded += 1
    ctx.log.info("full text: %d/%d article(s) extracted", upgraded, len(targets))


class RssSource(Source):
    description = "Articles from RSS/Atom feeds, optionally with full text fetched from each page."
    options_help = {
        "feeds": 'list of {"url", "name", "max_items" (10), "full_text" (false)} or plain URLs',
        "max_age_hours": "skip items older than this (default 36)",
        "max_articles": "cap for the whole edition (default 40)",
    }

    def collect(self, ctx: SourceContext) -> List[Section]:
        feeds = [_feed_spec(f) for f in ctx.option("feeds") or []]
        if not feeds:
            raise ValueError(f"project {ctx.project_id!r} has no feeds")
        cutoff = ctx.now - dt.timedelta(hours=float(ctx.option("max_age_hours", 36)))
        budget = int(ctx.option("max_articles", 40))
        sections: List[Section] = []
        for spec in feeds:
            if budget <= 0:
                break
            url = spec["url"]
            try:
                feed_title, items = parse(fetch(url))
            except Exception as e:
                ctx.log.warning("feed failed: %s (%s)", url, e)
                continue
            name = spec.get("name") or feed_title or urllib.parse.urlparse(url).netloc
            limit = min(int(spec.get("max_items", 10)), budget)
            section = Section(name=name)
            for item in items:
                if len(section.articles) >= limit:
                    break
                uid = item_uid(url, item)
                if uid in ctx.seen:
                    continue
                if item.published and item.published < cutoff:
                    continue
                body = clean_html(item.body)
                if not body and not item.link:
                    continue
                section.articles.append(Article(
                    uid=uid, title=item.title or "(untitled)", html=body, source=name,
                    link=item.link, author=item.author, published=item.published))
            ctx.log.info("feed %s: %d new item(s)", name, len(section.articles))
            if section.articles and spec.get("full_text"):
                _with_full_text(ctx, section.articles)
            if section.articles:
                budget -= len(section.articles)
                sections.append(section)
        return sections
