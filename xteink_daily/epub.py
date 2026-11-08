"""Minimal, deterministic EPUB 3 writer (stdlib only).

Deterministic output matters: KOReader sync matches books by a partial MD5 of
the file bytes, so every device must receive exactly the same file.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

CSS = """\
body { margin: 0; padding: 0; }
h1 { font-size: 1.6em; text-align: center; margin: 0.8em 0 0.2em; }
h2 { font-size: 1.3em; margin: 0.6em 0 0.3em; }
h3 { font-size: 1.15em; margin: 0.9em 0 0.3em; }
h4, h5 { font-size: 1em; font-weight: bold; margin: 0.9em 0 0.3em; }
p { margin: 0 0 0.6em; }
blockquote { margin: 0.6em 1.2em; }
pre { font-size: 0.85em; white-space: pre-wrap; }
hr { margin: 1em 0; }
.dateline, .summary { text-align: center; font-style: italic; }
.byline, .source { font-size: 0.85em; font-style: italic; }
.toc { list-style-type: none; padding-left: 0; }
.toc li { margin: 0 0 0.4em; }
.mins { font-size: 0.85em; }
"""


@dataclass
class EpubChapter:
    file: str                      # e.g. "c001.xhtml"
    title: str
    body: str                      # well-formed XHTML fragment
    section: Optional[str] = None  # groups chapters in the table of contents
    in_toc: bool = True


def _e(s: str) -> str:
    return html.escape(s or "", quote=True)


def xhtml_document(title: str, body: str, lang: str = "en") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{_e(lang)}" lang="{_e(lang)}">\n'
        f'<head><meta charset="utf-8"/><title>{_e(title)}</title>'
        '<link rel="stylesheet" type="text/css" href="../style.css"/></head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def is_well_formed(doc: str) -> bool:
    try:
        ET.fromstring(doc.encode("utf-8"))
        return True
    except ET.ParseError:
        return False


def _toc_groups(chapters: List[EpubChapter]):
    """[(section_title|None, [chapters])] preserving order."""
    groups: list = []
    for ch in chapters:
        if not ch.in_toc:
            continue
        if ch.section is None:
            groups.append((None, [ch]))
        elif groups and groups[-1][0] == ch.section:
            groups[-1][1].append(ch)
        else:
            groups.append((ch.section, [ch]))
    return groups


def _ncx(identifier: str, title: str, chapters: List[EpubChapter]) -> str:
    order = 0
    points: List[str] = []

    def point(label: str, src: str, children: str = "") -> str:
        nonlocal order
        order += 1
        return (f'<navPoint id="np{order}" playOrder="{order}"><navLabel><text>{_e(label)}</text>'
                f'</navLabel><content src="text/{src}"/>{children}</navPoint>')

    for section, chs in _toc_groups(chapters):
        if section is None:
            points.append(point(chs[0].title, chs[0].file))
        else:
            order += 1
            sec_order = order
            kids = "".join(point(c.title, c.file) for c in chs)
            points.append(f'<navPoint id="np{sec_order}" playOrder="{sec_order}"><navLabel><text>'
                          f'{_e(section)}</text></navLabel><content src="text/{chs[0].file}"/>'
                          f'{kids}</navPoint>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{_e(identifier)}"/><meta name="dtb:depth" content="2"/>'
        '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
        f"<docTitle><text>{_e(title)}</text></docTitle>\n"
        f"<navMap>{''.join(points)}</navMap>\n</ncx>\n"
    )


def _nav(title: str, chapters: List[EpubChapter], lang: str) -> str:
    items: List[str] = []
    for section, chs in _toc_groups(chapters):
        if section is None:
            items.append(f'<li><a href="text/{chs[0].file}">{_e(chs[0].title)}</a></li>')
        else:
            kids = "".join(f'<li><a href="text/{c.file}">{_e(c.title)}</a></li>' for c in chs)
            items.append(f'<li><a href="text/{chs[0].file}">{_e(section)}</a><ol>{kids}</ol></li>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{_e(lang)}" lang="{_e(lang)}">\n'
        f'<head><meta charset="utf-8"/><title>{_e(title)}</title></head>\n'
        f'<body><nav epub:type="toc" id="toc"><h1>{_e(title)}</h1><ol>{"".join(items)}</ol></nav></body>\n'
        "</html>\n"
    )


def _opf(*, identifier, title, language, date, modified, creator, publisher, description,
         chapters: List[EpubChapter]) -> str:
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []
    for i, ch in enumerate(chapters):
        cid = f"x{i:03d}"
        manifest.append(f'<item id="{cid}" href="text/{ch.file}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{cid}"/>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
        f'xml:lang="{_e(language)}">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'<dc:identifier id="bookid">{_e(identifier)}</dc:identifier>\n'
        f"<dc:title>{_e(title)}</dc:title>\n"
        f"<dc:creator>{_e(creator)}</dc:creator>\n"
        f"<dc:publisher>{_e(publisher)}</dc:publisher>\n"
        f"<dc:language>{_e(language)}</dc:language>\n"
        f"<dc:date>{date.isoformat()}</dc:date>\n"
        f"<dc:description>{_e(description)}</dc:description>\n"
        f'<meta property="dcterms:modified">{modified.strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>\n'
        "</metadata>\n"
        f"<manifest>\n{chr(10).join(manifest)}\n</manifest>\n"
        f'<spine toc="ncx">\n{chr(10).join(spine)}\n</spine>\n'
        "</package>\n"
    )


CONTAINER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles>\n</container>\n"
)


def build_epub(dest: Path, *, title: str, identifier: str, language: str, date: dt.date,
               modified: dt.datetime, creator: str, publisher: str, description: str,
               chapters: List[EpubChapter], css: str = CSS) -> int:
    """Write the EPUB atomically. Returns its size in bytes."""
    if not chapters:
        raise ValueError("an EPUB needs at least one chapter")
    files = [
        ("META-INF/container.xml", CONTAINER),
        ("OEBPS/content.opf", _opf(identifier=identifier, title=title, language=language, date=date,
                                   modified=modified.astimezone(dt.timezone.utc), creator=creator,
                                   publisher=publisher, description=description, chapters=chapters)),
        ("OEBPS/nav.xhtml", _nav(title, chapters, language)),
        ("OEBPS/toc.ncx", _ncx(identifier, title, chapters)),
        ("OEBPS/style.css", css),
    ]
    for ch in chapters:
        doc = xhtml_document(ch.title, ch.body, language)
        if not is_well_formed(doc):
            raise ValueError(f"chapter {ch.file} is not well-formed XHTML")
        files.append((f"OEBPS/text/{ch.file}", doc))
    for name, content in files:
        if name.endswith((".xhtml", ".opf", ".ncx", ".xml")) and not is_well_formed(content):
            raise ValueError(f"{name} is not well-formed XML")

    stamp = (max(date.year, 1980), date.month, date.day, 0, 0, 0)

    def info(name: str, compress: int) -> zipfile.ZipInfo:
        zi = zipfile.ZipInfo(name, date_time=stamp)
        zi.compress_type = compress
        zi.create_system = 3
        zi.external_attr = 0o644 << 16
        return zi

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        zf.writestr(info("mimetype", zipfile.ZIP_STORED), "application/epub+zip")
        for name, content in files:
            zf.writestr(info(name, zipfile.ZIP_DEFLATED), content.encode("utf-8"))
    os.replace(tmp, dest)
    return dest.stat().st_size
