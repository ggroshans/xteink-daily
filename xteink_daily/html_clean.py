"""Turn arbitrary feed HTML into a small, well-formed XHTML fragment.

E-reader parsers (CrossPoint's in particular) are strict XML parsers with
tight memory budgets. The output therefore uses a tiny tag vocabulary, never
nests blocks inside paragraphs, drops images/media/scripts, and is always
well-formed XML.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Union

_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[a-zA-Z][^>]*>|</[a-zA-Z][^>]*>|&[a-zA-Z#][a-zA-Z0-9]*;")

# Elements whose whole subtree is discarded.
DROP = {
    "script", "style", "iframe", "object", "video", "audio", "svg", "math", "form",
    "button", "select", "textarea", "noscript", "template", "head", "title", "nav",
    "canvas", "picture", "map", "dialog",
}
# Void elements that carry nothing we keep.
VOID = {"img", "input", "wbr", "link", "meta", "area", "col", "base", "param",
        "source", "track", "embed", "keygen"}

BLOCK_MAP = {
    "p": "p", "h1": "h3", "h2": "h3", "h3": "h4", "h4": "h5", "h5": "h5", "h6": "h5",
    "pre": "pre", "blockquote": "blockquote", "ul": "ul", "ol": "ol", "li": "li",
    "dt": "p", "dd": "p", "figcaption": "p",
}
INLINE_MAP = {
    "em": "em", "i": "em", "cite": "em", "var": "em", "strong": "strong", "b": "strong",
    "code": "code", "kbd": "code", "samp": "code", "tt": "code", "sub": "sub", "sup": "sup",
}
# Structural wrappers: dropped, but they end the current paragraph.
BOUNDARY = {
    "div", "section", "article", "main", "header", "footer", "aside", "figure", "table",
    "thead", "tbody", "tfoot", "tr", "caption", "address", "details", "summary",
    "center", "hgroup", "fieldset", "dl",
}

PARA = {"p", "h3", "h4", "h5", "pre"}
INLINE = {"em", "strong", "code", "sub", "sup"}
LISTS = {"ul", "ol"}


class _Node:
    __slots__ = ("tag", "children")

    def __init__(self, tag: str):
        self.tag = tag
        self.children: List[Union["_Node", str]] = []


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("body")
        self.stack: List[_Node] = [self.root]
        self.skip_tag: Optional[str] = None
        self.skip_depth = 0

    @property
    def top(self) -> _Node:
        return self.stack[-1]

    def _push(self, tag: str) -> None:
        node = _Node(tag)
        self.top.children.append(node)
        self.stack.append(node)

    def _close_while(self, tags: set) -> None:
        while len(self.stack) > 1 and self.top.tag in tags:
            self.stack.pop()

    def _in_pre(self) -> bool:
        return any(n.tag == "pre" for n in self.stack)

    def _ensure_inline_context(self) -> None:
        if self.top.tag in LISTS:
            self._push("li")
        if self.top.tag in ("body", "blockquote"):
            self._push("p")

    def _text(self, s: str) -> None:
        kids = self.top.children
        if kids and isinstance(kids[-1], str):
            kids[-1] += s
        else:
            kids.append(s)

    # -- parser callbacks -------------------------------------------------

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr") or tag in VOID:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in DROP:
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag == "br":
            if self.top.tag in PARA or self.top.tag in INLINE or self.top.tag == "li":
                self.top.children.append(_Node("br"))
            return
        if tag == "hr":
            self._close_while(PARA | INLINE | LISTS)
            self.top.children.append(_Node("hr"))
            return
        if tag in VOID:
            return
        if tag in BOUNDARY:
            self._close_while(PARA | INLINE)
            return
        out = BLOCK_MAP.get(tag)
        if out:
            if out in PARA:
                if out == "pre" and self._in_pre():
                    return
                self._close_while(PARA | INLINE | LISTS)
                self._push(out)
            elif out in LISTS:
                self._close_while(PARA | INLINE)
                self._push(out)
            elif out == "li":
                self._close_while(PARA | INLINE)
                if self.top.tag == "li":
                    self.stack.pop()
                if self.top.tag not in LISTS:
                    self._push("ul")
                self._push("li")
            elif out == "blockquote":
                self._close_while(PARA | INLINE | LISTS)
                self._push("blockquote")
            return
        out = INLINE_MAP.get(tag)
        if out:
            self._ensure_inline_context()
            self._push(out)
        # Anything else (a, span, font, u, ...) is transparent: text kept, tag dropped.

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in BOUNDARY:
            self._close_while(PARA | INLINE)
            return
        if tag in ("td", "th"):
            if self.top.tag in PARA or self.top.tag in INLINE or self.top.tag == "li":
                self._text(" ")
            return
        out = BLOCK_MAP.get(tag) or INLINE_MAP.get(tag)
        if not out or (out == "pre" and tag != "pre"):
            return
        for i in range(len(self.stack) - 1, 0, -1):
            node_tag = self.stack[i].tag
            if node_tag == out:
                del self.stack[i:]
                return
            # An inline or paragraph end tag never closes an enclosing list item
            # or quote (the element it names was already closed implicitly).
            if (out in INLINE or out in PARA) and node_tag not in INLINE:
                return

    def handle_data(self, data):
        if self.skip_tag or not data:
            return
        if not self._in_pre():
            data = _WS.sub(" ", data)
            if data == " " and (self.top.tag in ("body", "blockquote") or self.top.tag in LISTS):
                return
        self._ensure_inline_context()
        self._text(data)


def _has_content(node: _Node) -> bool:
    for c in node.children:
        if isinstance(c, str):
            if c.strip():
                return True
        elif c.tag == "hr" or (c.tag != "br" and _has_content(c)):
            return True
    return False


def _serialize(node: _Node, out: List[str]) -> None:
    for c in node.children:
        if isinstance(c, str):
            out.append(html.escape(_ILLEGAL_XML.sub("", c), quote=False))
        elif c.tag in ("br", "hr"):
            out.append(f"<{c.tag}/>")
        elif _has_content(c):
            out.append(f"<{c.tag}>")
            _serialize(c, out)
            out.append(f"</{c.tag}>")


def looks_like_html(s: str) -> bool:
    return bool(_TAG.search(s or ""))


def text_to_html(text: str) -> str:
    """Plain text -> paragraphs (blank line = new paragraph, newline = <br/>)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n"))]
    out = []
    for p in paras:
        if p:
            lines = [html.escape(_ILLEGAL_XML.sub("", ln.strip()), quote=False) for ln in p.split("\n")]
            out.append("<p>" + "<br/>".join(ln for ln in lines if ln) + "</p>")
    return "".join(out)


def clean_html(fragment: str) -> str:
    """Sanitize an HTML fragment into well-formed, minimal XHTML."""
    if not looks_like_html(fragment):
        return text_to_html(html.unescape(fragment or ""))
    cleaner = _Cleaner()
    try:
        cleaner.feed(fragment)
        cleaner.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient; belt and braces
        return text_to_html(html_to_text(fragment))
    out: List[str] = []
    _serialize(cleaner.root, out)
    return "".join(out).strip()


def html_to_text(fragment: str) -> str:
    """Rough plain-text rendering, used for word counts and fallbacks."""
    no_tags = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", fragment or "", flags=re.S | re.I)
    no_tags = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", no_tags, flags=re.I)
    no_tags = re.sub(r"</?[a-zA-Z][^<>]*>", " ", no_tags)
    text = html.unescape(no_tags)
    return "\n".join(_WS.sub(" ", ln).strip() for ln in text.split("\n") if ln.strip())


def inline_text(fragment: str) -> str:
    """Single-line plain text (for titles that arrive as HTML)."""
    return _WS.sub(" ", html_to_text(fragment)).strip()


def word_count(fragment: str) -> int:
    return len(html_to_text(fragment).split())
