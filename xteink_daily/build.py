"""Assemble one project's daily edition into an EPUB."""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import APP
from .config import Project
from .content import Article, Section
from .context import SourceContext
from .epub import EpubChapter, build_epub, is_well_formed, xhtml_document
from .html_clean import html_to_text, text_to_html, word_count
from .library import Edition, Library
from .llm import LLM
from .sources import BUILTIN, instantiate

SLEEP_DOWNLOAD_LIMIT = 1_000_000  # CrossPoint event downloads are capped at 1MB
SEEN_RETENTION_DAYS = 180         # how long an item's uid blocks it from reappearing
WORDS_PER_MINUTE = 230


@dataclass
class BuildResult:
    project: Project
    status: str  # built | exists | empty | locked | failed
    edition: Optional[Edition] = None
    message: str = ""


def _e(s: str) -> str:
    return html.escape(s or "", quote=True)


def _minutes(words: int) -> int:
    return max(1, round(words / WORDS_PER_MINUTE))


def _long_date(d: dt.date) -> str:
    return f"{d:%A}, {d.day} {d:%B %Y}"


def _short_date(d: dt.date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _byline(art: Article) -> str:
    if art.byline is not None:
        return art.byline
    meta = [art.source]
    if art.author and art.author != art.source:
        meta.append(art.author)
    if art.published:
        local = art.published.astimezone()
        meta.append(f"{_short_date(local.date())}, {local:%H:%M}")
    return " · ".join(m for m in meta if m)


def _safe_body(title: str, body: str, lang: str) -> str:
    if is_well_formed(xhtml_document(title, body, lang)):
        return body
    # Sources are expected to emit well-formed XHTML (clean_html does); degrade to text if not.
    return text_to_html(html_to_text(body))


def _article_chapters(n: int, art: Article, section: str, lang: str) -> List[EpubChapter]:
    byline = _byline(art)
    parts = [f"<h2>{_e(art.title)}</h2>"]
    if byline:
        parts.append(f'<p class="byline">{_e(byline)}</p>')
    parts.append(_safe_body(art.title, art.html, lang))
    if art.link and not art.reveal:
        host = urllib.parse.urlparse(art.link).netloc or art.link
        parts.append(f'<p class="source">Source: <a href="{_e(art.link)}">{_e(host)}</a></p>')
    chapters = [EpubChapter(file=f"c{n:03d}.xhtml", title=art.title,
                            body="\n".join(p for p in parts if p), section=section)]
    if art.reveal:
        reveal = [_safe_body(art.title, art.reveal, lang)]
        if art.link:
            host = urllib.parse.urlparse(art.link).netloc or art.link
            reveal.append(f'<p class="source">Source: <a href="{_e(art.link)}">{_e(host)}</a></p>')
        chapters.append(EpubChapter(file=f"c{n:03d}r.xhtml", title=art.title, body="\n".join(reveal),
                                    section=section, in_toc=False))
    return chapters


def _contents_page(project: Project, date: dt.date, sections: List[Section],
                   first_files: List[str], total_words: int) -> str:
    n_articles = sum(len(s.articles) for s in sections)
    out = [f"<h1>{_e(project.title)}</h1>",
           f'<p class="dateline">{_e(_long_date(date))}</p>',
           f'<p class="summary">{n_articles} item{"s" if n_articles != 1 else ""} · '
           f"about {_minutes(total_words)} min</p>"]
    i = 0
    for s in sections:
        if len(sections) > 1 or s.name != project.title:
            out.append(f"<h2>{_e(s.name)}</h2>")
        out.append('<ul class="toc">')
        for art in s.articles:
            out.append(f'<li><a href="{first_files[i]}">{_e(art.title)}</a> '
                       f'<span class="mins">· {_minutes(word_count(art.html))} min</span></li>')
            i += 1
        out.append("</ul>")
    return "\n".join(out)


def build_tools(cfg, library: Library, log: Optional[logging.Logger] = None):
    """(source registry, LLM) for a config: create once per process, pass to build_project."""
    from .sources import load_sources
    return load_sources(cfg, log), LLM(cfg.llm, library.root / ".cache" / "llm", log)


def build_project(project: Project, library: Library, *, now: Optional[dt.datetime] = None,
                  force: bool = False, log: Optional[logging.Logger] = None,
                  sources: Optional[Dict[str, Any]] = None, llm: Optional[LLM] = None,
                  cfg=None) -> BuildResult:
    log = log or logging.getLogger(APP)
    now = now or dt.datetime.now().astimezone()
    date = now.date()

    existing = library.find(project, date)
    if existing and not force:
        return BuildResult(project, "exists", existing)
    if existing and force:
        with library.state() as st:
            targets = [t for t, names in st["delivered"].items() if existing.filename in names]
        if targets:
            return BuildResult(project, "locked", existing,
                               f"already delivered to {', '.join(targets)}; rebuilding would change "
                               "the file and break KOReader sync for it")

    with library.state() as st:
        seen = frozenset(st["seen"].get(project.id, {}))

    ctx = SourceContext(project_id=project.id, title=project.title, source_type=project.type,
                        options=project.options, now=now, seen=seen,
                        cache_root=library.root / ".cache", llm=llm, log=log,
                        config_dir=cfg.path.parent if cfg is not None else None)
    try:
        source = instantiate(sources if sources is not None else BUILTIN, project.type, cfg)
        sections = [s for s in source.collect(ctx) if s.articles]
    except Exception as e:
        log.exception("project %s failed", project.id)
        return BuildResult(project, "failed", message=str(e))

    articles = [a for s in sections for a in s.articles]
    if not articles:
        return BuildResult(project, "empty", message="nothing new")

    lang = str(project.options.get("language", "en"))
    chapters: List[EpubChapter] = []
    first_files: List[str] = []
    total_words = 0
    n = 1
    for s in sections:
        for art in s.articles:
            chs = _article_chapters(n, art, s.name, lang)
            first_files.append(chs[0].file)
            chapters.extend(chs)
            total_words += word_count(art.html) + word_count(art.reveal)
            n += 1
    contents = EpubChapter(file="contents.xhtml", title="Contents",
                           body=_contents_page(project, date, sections, first_files, total_words))

    title = f"{project.title} — {_short_date(date)}"
    identifier = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"{APP}:{project.id}:{date}"))
    noun = "item" if len(articles) == 1 else "items"
    description = f"{len(articles)} {noun} from " + ", ".join(s.name for s in sections)
    path = library.edition_path(project, date)
    size = build_epub(path, title=title, identifier=identifier, language=lang, date=date,
                      modified=now, creator=project.title, publisher=APP, description=description,
                      chapters=[contents] + chapters)

    edition = Edition(project.id, date, path)
    edition.meta_path.write_text(json.dumps({
        "title": title,
        "project": project.id,
        "type": project.type,
        "date": date.isoformat(),
        "identifier": identifier,
        "built_at": now.isoformat(timespec="seconds"),
        "articles": len(articles),
        "minutes": _minutes(total_words),
        "sections": [{"name": s.name, "articles": len(s.articles)} for s in sections],
        "description": description,
        "size": size,
    }, indent=1), encoding="utf-8")

    with library.state() as st:
        project_seen = st["seen"].setdefault(project.id, {})
        for a in articles:
            project_seen[a.uid] = date.isoformat()
        horizon = (date - dt.timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
        st["seen"][project.id] = {k: v for k, v in project_seen.items() if v >= horizon}

    msg = f"{len(articles)} {noun}, {size // 1024} KB"
    if size > SLEEP_DOWNLOAD_LIMIT:
        msg += " (over 1MB: the sleep-pull plugin will skip it; push/OPDS still deliver it)"
        log.warning("%s: %s", path.name, msg)
    return BuildResult(project, "built", edition, msg)
