"""Content types produced by sources and turned into EPUB chapters."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Article:
    """One item in an edition; becomes one chapter (plus an optional reveal page).

    uid        Stable id. Items whose uid appeared in an earlier edition are passed to
               the source in ctx.seen so it can skip them (no repeats across days).
    title      Chapter title (plain text).
    html       Body as an XHTML fragment. Run untrusted HTML through clean_html().
    reveal     Optional XHTML shown on the *next* page: answers, definitions, spoilers.
    byline     Line under the title. None = built from source/author/published; "" = none.
    link       Original URL, printed as "Source: <domain>" at the end.
    """
    uid: str
    title: str
    html: str
    source: str = ""
    link: str = ""
    author: str = ""
    published: Optional[dt.datetime] = None
    reveal: str = ""
    byline: Optional[str] = None


@dataclass
class Section:
    """A named group of articles (a heading in the table of contents)."""
    name: str
    articles: List[Article] = field(default_factory=list)
