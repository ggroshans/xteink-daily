"""Public API for content sources. Content packs should import only from here.

    from xteink_daily.api import Article, Section, Source

    class DailyQuote(Source):
        description = "One quote a day."
        options_help = {"feed": "URL of a quotes JSON feed"}

        def collect(self, ctx):
            q = ctx.get_json(ctx.option("feed"))
            return [Section("Quote", [Article(uid=q["id"], title=q["author"],
                                              html=text_to_html(q["text"]))])]

    SOURCES = {"daily-quote": DailyQuote}   # in your module; list it under "plugins" in config.json

Everything here is stable within API_VERSION. See docs/writing-sources.md.
"""
from __future__ import annotations

from typing import Dict, List

from .content import Article, Section
from .context import SourceContext
from .html_clean import clean_html, html_to_text, inline_text, text_to_html, word_count

API_VERSION = 1


class Source:
    """Base class for content sources (subclassing is optional: any object with collect() works).

    description    one line shown by `xteink-daily sources`
    options_help   {option: help text} shown by `xteink-daily sources`
    """

    description: str = ""
    options_help: Dict[str, str] = {}

    def collect(self, ctx: SourceContext) -> List[Section]:  # pragma: no cover - interface
        raise NotImplementedError


__all__ = ["API_VERSION", "Article", "Section", "Source", "SourceContext",
           "clean_html", "html_to_text", "inline_text", "text_to_html", "word_count"]
