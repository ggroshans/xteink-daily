"""Run any program as a content source (any language).

Project options:
    command   ["python3", "my_source.py"] or a shell-style string; run with cwd = the
              config file's folder unless "cwd" is given
    timeout   seconds (default 300)
    ...       everything else is passed through to the program

The program receives one JSON object on stdin:
    {"api": 1, "project": "...", "title": "...", "date": "2026-11-08",
     "now": "2026-11-08T05:30:00-05:00", "options": {...}, "seen": ["uid", ...]}

and prints one JSON object on stdout, either {"sections": [...]} or, for a single
section, {"name": "...", "articles": [...]}. Each article:
    {"id": "stable-id", "title": "...", "html": "<p>...</p>" | "text": "plain text",
     "reveal_html" | "reveal_text": "shown on the next page" (optional),
     "byline": "...", "link": "https://...", "author": "...", "published": "ISO-8601"}
HTML is sanitized. Anything on stderr is logged. A non-zero exit fails the build.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from ..api import API_VERSION, Article, Section, Source, SourceContext
from ..html_clean import clean_html, inline_text, text_to_html
from .rss import parse_date


def _body(item: Dict[str, Any], html_key: str, text_key: str) -> str:
    if item.get(html_key):
        return clean_html(str(item[html_key]))
    if item.get(text_key):
        return text_to_html(str(item[text_key]))
    return ""


def parse_output(data: Any, source_name: str) -> List[Section]:
    if not isinstance(data, dict):
        raise ValueError("command output must be a JSON object")
    raw_sections = data.get("sections")
    if raw_sections is None:
        raw_sections = [{"name": data.get("name") or source_name, "articles": data.get("articles") or []}]
    sections: List[Section] = []
    for s in raw_sections:
        arts = []
        for i, a in enumerate(s.get("articles") or []):
            title = inline_text(str(a.get("title") or "")) or "(untitled)"
            uid = str(a.get("id") or a.get("uid") or f"{title}:{i}")
            byline = a.get("byline")
            arts.append(Article(
                uid=uid, title=title, html=_body(a, "html", "text"),
                reveal=_body(a, "reveal_html", "reveal_text"),
                source=str(a.get("source") or s.get("name") or source_name),
                link=str(a.get("link") or ""), author=str(a.get("author") or ""),
                published=parse_date(a.get("published")),
                byline=None if byline is None else inline_text(str(byline))))
        if arts:
            sections.append(Section(name=str(s.get("name") or source_name), articles=arts))
    return sections


class CommandSource(Source):
    description = "Runs any program (any language) that prints articles as JSON."
    options_help = {
        "command": 'program and arguments, e.g. ["python3", "quotes.py"]',
        "timeout": "seconds before the program is stopped (default 300)",
        "cwd": "working directory (default: the config file's folder)",
    }

    def __init__(self, config_dir: Path = Path.cwd()):
        self.config_dir = config_dir

    def collect(self, ctx: SourceContext) -> List[Section]:
        command = ctx.option("command")
        if isinstance(command, str):
            command = shlex.split(command)
        if not command or not isinstance(command, list):
            raise ValueError(f"project {ctx.project_id!r}: set \"command\" to the program to run")
        payload = {
            "api": API_VERSION, "project": ctx.project_id, "title": ctx.title,
            "date": ctx.date.isoformat(), "now": ctx.now.isoformat(),
            "options": ctx.options, "seen": sorted(ctx.seen),
        }
        cwd = Path(ctx.option("cwd") or self.config_dir).expanduser()
        proc = subprocess.run([str(c) for c in command], input=json.dumps(payload), cwd=str(cwd),
                              capture_output=True, text=True, timeout=float(ctx.option("timeout", 300)))
        for line in (proc.stderr or "").splitlines():
            ctx.log.info("[%s] %s", ctx.project_id, line)
        if proc.returncode != 0:
            raise RuntimeError(f"command exited with status {proc.returncode}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ValueError(f"command did not print valid JSON: {e}") from None
        return parse_output(data, ctx.title)
