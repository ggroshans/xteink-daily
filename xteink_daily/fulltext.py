"""Fetch an article page and pull out its main text (a small readability-style heuristic).

Used when a feed only carries summaries. It scores containers by the amount of
paragraph text they hold, picks the best one, and returns its HTML for the
sanitizer. Callers fall back to the feed summary when extraction looks wrong
(paywalls, JavaScript-only pages).
"""
from __future__ import annotations

import html
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Dict, List, Optional

from .net import http_get

MAX_PAGE_BYTES = 4 * 1024 * 1024
SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "form", "button", "select",
        "textarea", "nav", "footer", "aside", "head", "canvas", "video", "audio", "picture", "dialog"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
BLOCKS = {"p", "div", "section", "article", "main", "ul", "ol", "li", "blockquote", "pre", "table",
          "h1", "h2", "h3", "h4", "h5", "h6", "figure", "header", "dl"}
POSITIVE = re.compile(r"article|body|content|entry|main|post|story|text|prose", re.I)
NEGATIVE = re.compile(r"comment|footer|sidebar|related|promo|newsletter|share|social|subscribe|"
                      r"advert|\bad-|banner|byline|caption|breadcrumb|menu|nav|popup|modal|"
                      r"recommend|most-read|outbrain|taboola|signup|cookie", re.I)


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["_Node"]):
        self.tag = tag
        self.attrs = attrs
        self.children: List = []
        self.parent = parent

    @property
    def hint(self) -> str:
        return f"{self.attrs.get('class', '')} {self.attrs.get('id', '')}"


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, None)
        self.stack = [self.root]
        self.skip_depth = 0
        self.skip_tag = ""

    def handle_starttag(self, tag, attrs):
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in SKIP:
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag in VOID:
            if tag == "br":
                self.stack[-1].children.append("\n")
            return
        if tag in BLOCKS and any(n.tag == "p" for n in self.stack):
            while self.stack[-1].tag != "p":
                self.stack.pop()
            self.stack.pop()
        node = _Node(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_endtag(self, tag):
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self.skip_depth:
            self.stack[-1].children.append(data)


def _text(node: _Node) -> str:
    out = []
    for c in node.children:
        out.append(c if isinstance(c, str) else _text(c))
    return "".join(out)


def _link_text_len(node: _Node) -> int:
    total = 0
    for c in node.children:
        if isinstance(c, _Node):
            total += len(_text(c)) if c.tag == "a" else _link_text_len(c)
    return total


def _walk(node: _Node):
    for c in node.children:
        if isinstance(c, _Node):
            yield c
            yield from _walk(c)


def _to_html(node: _Node) -> str:
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(html.escape(c, quote=False))
        elif NEGATIVE.search(c.hint) and not POSITIVE.search(c.hint):
            continue
        else:
            out.append(f"<{c.tag}>{_to_html(c)}</{c.tag}>")
    return "".join(out)


def extract(page_html: str) -> Optional[str]:
    """Return the main content as an HTML fragment, or None if nothing convincing was found."""
    builder = _TreeBuilder()
    try:
        builder.feed(page_html)
        builder.close()
    except Exception:
        return None
    scores: Dict[int, float] = {}
    nodes: Dict[int, _Node] = {}
    for node in _walk(builder.root):
        if node.tag != "p":
            continue
        text = " ".join(_text(node).split())
        if len(text) < 40 or _link_text_len(node) > 0.5 * len(text):
            continue
        score = 1 + min(len(text) / 100, 3) + text.count(",") * 0.5
        parent = node.parent
        for weight in (1.0, 0.5):
            if parent is None or parent.tag == "root":
                break
            nodes[id(parent)] = parent
            scores[id(parent)] = scores.get(id(parent), 0) + score * weight
            parent = parent.parent
    if not scores:
        return None
    for key, node in nodes.items():
        if POSITIVE.search(node.hint):
            scores[key] *= 1.25
        if NEGATIVE.search(node.hint):
            scores[key] *= 0.3
        if node.tag == "article":
            scores[key] *= 1.3
    best_key = max(scores, key=scores.__getitem__)
    best = nodes[best_key]
    return "".join(f"<{n.tag}>{_to_html(n)}</{n.tag}>" for n in _with_siblings(best, scores[best_key], scores))


def _with_siblings(best: _Node, best_score: float, scores: Dict[int, float]) -> List[_Node]:
    """Stories split across sibling blocks (e.g. NPR's repeated <div class="content">)
    are reassembled: keep scored siblings sharing best's tag+class, or scoring nearly as well."""
    parent = best.parent
    if parent is None or parent.tag == "root":
        return [best]
    best_class = best.attrs.get("class", "")
    picked = []
    for sib in parent.children:
        if not isinstance(sib, _Node):
            continue
        if sib is best:
            picked.append(sib)
            continue
        score = scores.get(id(sib))
        if score is None or (NEGATIVE.search(sib.hint) and not POSITIVE.search(sib.hint)):
            continue
        same_kind = bool(best_class) and sib.tag == best.tag and sib.attrs.get("class", "") == best_class
        if same_kind or score >= 0.3 * best_score:
            picked.append(sib)
    return picked


def fetch_article(url: str, timeout: float = 20.0) -> Optional[str]:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return None
    raw, headers = http_get(url, accept="text/html,*/*;q=0.5", max_bytes=MAX_PAGE_BYTES, timeout=timeout)
    if "html" not in headers.get("Content-Type", ""):
        return None
    charset = headers.get_content_charset() or "utf-8"
    try:
        page = raw.decode(charset, errors="replace")
    except LookupError:
        page = raw.decode("utf-8", errors="replace")
    result = extract(page)
    # Undecodable bytes mean we're looking at binary or mis-declared content, not prose.
    if result and result.count("�") > 3:
        return None
    return result
