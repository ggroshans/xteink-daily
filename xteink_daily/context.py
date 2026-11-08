"""SourceContext: everything a content source gets to work with."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, FrozenSet, Iterable, List, Optional, TypeVar

from .fulltext import fetch_article
from .html_clean import clean_html, html_to_text
from .llm import LLM, extractive_summary
from .net import http_get

T = TypeVar("T")
R = TypeVar("R")


class SourceContext:
    """Passed to Source.collect(). Attributes:

        project_id, title   the project being built
        options             the project's entries from config.json (everything except id/title/type)
        now, date           build time (timezone-aware) and edition date
        seen                uids used in earlier editions of this project: skip these
        rng                 random.Random seeded by project + date: same picks if rebuilt that day
        cache_dir           persistent per-source-type folder for downloads (datasets, word lists)
        config_dir          folder holding config.json (resolve relative paths in options with path())
        log                 logging.Logger
    """

    def __init__(self, *, project_id: str, title: str, source_type: str, options: dict,
                 now: dt.datetime, seen: FrozenSet[str], cache_root: Path,
                 llm: Optional[LLM] = None, log: Optional[logging.Logger] = None,
                 config_dir: Optional[Path] = None):
        self.config_dir = config_dir or Path.cwd()
        self.project_id = project_id
        self.title = title
        self.source_type = source_type
        self.options = options
        self.now = now
        self.date = now.date()
        self.seen = seen
        self.log = log or logging.getLogger(f"xteink-daily.{source_type}")
        self.rng = random.Random(f"{project_id}:{self.date.isoformat()}")
        self.cache_dir = cache_root / "sources" / source_type
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._llm = llm

    # -- options ----------------------------------------------------------

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    def path(self, value: str) -> Path:
        """Resolve a path from the options: ~ expanded, relative to config.json's folder."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.config_dir / p).resolve()

    # -- HTTP -------------------------------------------------------------

    def get(self, url: str, *, accept: str = "*/*", max_bytes: int = 16 << 20, timeout: float = 20.0) -> bytes:
        return http_get(url, accept=accept, max_bytes=max_bytes, timeout=timeout)[0]

    def get_text(self, url: str, **kw) -> str:
        data, headers = http_get(url, accept=kw.pop("accept", "text/*,*/*;q=0.5"),
                                 max_bytes=kw.pop("max_bytes", 16 << 20), timeout=kw.pop("timeout", 20.0))
        charset = headers.get_content_charset() or "utf-8"
        try:
            return data.decode(charset, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")

    def get_json(self, url: str, **kw) -> Any:
        return json.loads(self.get(url, accept="application/json", **kw))

    def download(self, url: str, name: str, *, max_age_days: Optional[float] = None,
                 max_bytes: int = 256 << 20) -> Path:
        """Fetch url into cache_dir/name once (or again after max_age_days) and return the path."""
        if "/" in name or name.startswith("."):
            raise ValueError(f"bad cache file name: {name!r}")
        path = self.cache_dir / name
        if path.exists():
            age_days = (time.time() - path.stat().st_mtime) / 86400
            if max_age_days is None or age_days < max_age_days:
                return path
        data = self.get(url, max_bytes=max_bytes, timeout=120.0)
        tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.log.info("downloaded %s (%d KB)", name, len(data) // 1024)
        return path

    def article_html(self, url: str) -> Optional[str]:
        """Fetch a web page and extract its main text as clean XHTML (None if it can't)."""
        try:
            extracted = fetch_article(url)
        except Exception as e:
            self.log.debug("article fetch failed for %s: %s", url, e)
            return None
        return clean_html(extracted) if extracted else None

    # -- LLM --------------------------------------------------------------

    @property
    def has_llm(self) -> bool:
        return bool(self._llm and self._llm.available)

    def ask(self, prompt: str, system: Optional[str] = None) -> Optional[str]:
        """Send a prompt to the configured LLM. Returns None when no LLM is available,
        so always have a fallback."""
        return self._llm.ask(prompt, system) if self._llm else None

    def summarize(self, text: str, instruction: str = "Summarize the key points.",
                  max_words: int = 120) -> str:
        """LLM summary when available, otherwise the leading sentences of `text`."""
        text = html_to_text(text) if "<" in text else text
        if not text.strip():
            return ""
        if self.has_llm:
            result = self.ask(f"{instruction}\nUse at most {max_words} words.\n\n<text>\n{text}\n</text>")
            if result:
                return result
        return extractive_summary(text, max_words)

    # -- concurrency ------------------------------------------------------

    def map(self, fn: Callable[[T], R], items: Iterable[T], workers: int = 6) -> List[Optional[R]]:
        """Run fn over items in parallel; an item whose fn raises becomes None (and is logged)."""
        def safe(item: T) -> Optional[R]:
            try:
                return fn(item)
            except Exception as e:
                self.log.warning("%s: item failed: %s", self.source_type, e)
                return None

        items = list(items)
        if len(items) <= 1 or workers <= 1:
            return [safe(i) for i in items]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(safe, items))
