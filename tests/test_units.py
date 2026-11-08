import datetime as dt
import hashlib
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from xteink_daily.epub import EpubChapter, build_epub, is_well_formed, xhtml_document
from xteink_daily.fulltext import extract
from xteink_daily.html_clean import clean_html, inline_text, text_to_html, word_count
from xteink_daily.kosync import partial_md5
from xteink_daily.sources.rss import parse, parse_date


def wf(fragment: str) -> bool:
    return is_well_formed(xhtml_document("t", fragment))


class HtmlCleanTests(unittest.TestCase):
    def test_malformed_html_becomes_well_formed(self):
        out = clean_html("<p>one<p>two<div>three</p><ul><li>a<li>b</ul><script>bad()</script>"
                         "<em>x<strong>y</em>z</strong></li></p><table><tr><td>c1</td><td>c2</td></tr></table>")
        self.assertTrue(wf(out), out)
        self.assertIn("<p>one</p><p>two</p><p>three</p>", out)
        self.assertIn("<ul><li>a</li><li>b</li></ul>", out)
        self.assertNotIn("bad()", out)
        self.assertIn("c1", out)

    def test_blocks_never_nest_in_paragraphs(self):
        out = clean_html("<p>intro<blockquote>quote</blockquote>after</p><h1>Big</h1>")
        self.assertTrue(wf(out), out)
        root = ET.fromstring(f"<body>{out}</body>")
        for p in root.iter("p"):
            self.assertFalse([c for c in p if c.tag in ("p", "blockquote", "ul", "h3")])
        self.assertIn("<h3>Big</h3>", out)

    def test_entities_and_control_chars(self):
        out = clean_html("<p>a&nbsp;&amp;&lt;b&gt; \x07 caf&eacute;</p>")
        self.assertTrue(wf(out), out)
        self.assertIn("&amp;&lt;b&gt;", out)
        self.assertIn("café", out)

    def test_plain_text(self):
        self.assertEqual(text_to_html("one\ntwo\n\nthree & four"), "<p>one<br/>two</p><p>three &amp; four</p>")
        self.assertEqual(clean_html("just text"), "<p>just text</p>")

    def test_drops_media_and_empty(self):
        out = clean_html('<p><img src="x.jpg"></p><iframe><p>no</p></iframe><p> </p><p>kept</p>')
        self.assertEqual(out, "<p>kept</p>")

    def test_inline_text_keeps_comparisons(self):
        self.assertEqual(inline_text("Why x < y &amp; <b>z</b>"), "Why x < y & z")

    def test_word_count(self):
        self.assertEqual(word_count("<p>one two</p><p>three</p>"), 3)


RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Test &amp; Feed</title>
<item><title>First &amp;amp; best</title><link>https://example.com/1</link>
<guid>id-1</guid><pubDate>Sat, 07 Nov 2026 10:00:00 +0000</pubDate>
<description>Short</description>
<content:encoded><![CDATA[<p>Full&nbsp;text <b>here</b></p>]]></content:encoded></item>
<item><title>Second</title><link>https://example.com/2?a=1&b=2</link><description>Fish &nbsp; chips</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title type="html">Atom &lt;i&gt;feed&lt;/i&gt;</title>
<entry><id>urn:1</id><title>Entry</title><updated>2026-11-08T06:00:00.123Z</updated>
<link rel="alternate" type="text/html" href="https://example.org/e1"/>
<author><name>Ada</name></author>
<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Hello <em>world</em></p></div></content>
</entry></feed>"""


class FeedTests(unittest.TestCase):
    def test_rss_with_html_entities_and_bare_ampersands(self):
        title, items = parse(RSS)
        self.assertEqual(title, "Test & Feed")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "First & best")
        self.assertIn("<b>here</b>", items[0].body)
        self.assertEqual(items[0].published, dt.datetime(2026, 11, 7, 10, tzinfo=dt.timezone.utc))
        self.assertEqual(items[1].link, "https://example.com/2?a=1&b=2")

    def test_atom_xhtml_content(self):
        title, items = parse(ATOM)
        self.assertEqual(title, "Atom feed")
        self.assertEqual(items[0].link, "https://example.org/e1")
        self.assertEqual(items[0].author, "Ada")
        self.assertEqual(clean_html(items[0].body), "<p>Hello <em>world</em></p>")

    def test_dates(self):
        self.assertEqual(parse_date("2026-11-08T06:00:00+02:00").utcoffset(), dt.timedelta(hours=2))
        self.assertEqual(parse_date("2026-11-08").year, 2026)
        self.assertIsNotNone(parse_date("Sun, 8 Nov 2026 07:00:00 GMT"))
        self.assertIsNone(parse_date("not a date"))

    def test_rejects_non_feed(self):
        with self.assertRaises(ValueError):
            parse(b"<html><body>nope</body></html>")

    def test_retries_rate_limits_and_server_errors(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from xteink_daily.net import http_get
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                if self.path == "/flaky" and len(hits) == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                code = 404 if self.path == "/missing" else 200
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            self.assertEqual(http_get(base + "/flaky", accept="*/*", max_bytes=100)[0], b"ok")
            self.assertEqual(len(hits), 2)
            import urllib.error
            with self.assertRaises(urllib.error.HTTPError):  # 404 is not retried
                http_get(base + "/missing", accept="*/*", max_bytes=100)
            self.assertEqual(len(hits), 3)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_gzip_body_sent_without_being_asked(self):
        # npr.org gzips responses even without Accept-Encoding; we must decode by magic bytes.
        import gzip
        from xteink_daily.sources.rss import fetch
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            f.write(gzip.compress(RSS))
        try:
            self.assertEqual(len(parse(fetch(Path(f.name).as_uri()))[1]), 2)
        finally:
            Path(f.name).unlink()


class EpubTests(unittest.TestCase):
    def _build(self, path: Path):
        chapters = [EpubChapter("contents.xhtml", "Contents", "<h1>T</h1>"),
                    EpubChapter("c001.xhtml", "A & B", "<h2>A &amp; B</h2><p>x</p>", section="News"),
                    EpubChapter("c002.xhtml", "C", "<p>y</p>", section="News"),
                    EpubChapter("c003.xhtml", "D", "<p>z</p>", section="Tech")]
        return build_epub(path, title="Ed — Sun 8 Nov", identifier="urn:uuid:x", language="en",
                          date=dt.date(2026, 11, 8), modified=dt.datetime(2026, 11, 8, 5, 30, tzinfo=dt.timezone.utc),
                          creator="Ed", publisher="xteink-daily", description="d", chapters=chapters)

    def test_structure_and_determinism(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.epub", Path(tmp) / "b.epub"
            self._build(a)
            self._build(b)
            self.assertEqual(a.read_bytes(), b.read_bytes(), "builds must be byte-identical")
            with zipfile.ZipFile(a) as z:
                first = z.infolist()[0]
                self.assertEqual(first.filename, "mimetype")
                self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(z.read("mimetype"), b"application/epub+zip")
                for name in z.namelist():
                    if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                        ET.fromstring(z.read(name))
                ncx = z.read("OEBPS/toc.ncx").decode()
                self.assertIn("<text>News</text>", ncx)
                orders = [int(x) for x in __import__("re").findall(r'playOrder="(\d+)"', ncx)]
                self.assertEqual(sorted(orders), list(range(1, len(orders) + 1)))

    def test_rejects_malformed_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_epub(Path(tmp) / "x.epub", title="t", identifier="i", language="en",
                           date=dt.date(2026, 1, 1), modified=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                           creator="c", publisher="p", description="d",
                           chapters=[EpubChapter("c.xhtml", "t", "<p>unclosed")])


class FullTextTests(unittest.TestCase):
    def test_picks_article_body(self):
        para = "<p>" + "This is a substantial sentence, with commas, about the news. " * 3 + "</p>"
        page = (f"<html><body><nav><p>{'menu ' * 30}</p></nav><div class='sidebar'>{para}</div>"
                f"<article class='story-body'><h1>Headline</h1>{para * 5}"
                f"<div class='related'>{para}</div></article><footer>{para}</footer></body></html>")
        out = extract(page)
        self.assertIsNotNone(out)
        self.assertEqual(clean_html(out).count("<p>"), 5)
        self.assertNotIn("menu", out)

    def test_reassembles_story_split_across_sibling_blocks(self):
        para = "<p>" + "A long sentence of story text, with some commas, for scoring. " * 3 + "</p>"
        page = (f"<main><div class='content'>{para * 3}</div><figure>chart</figure>"
                f"<div class='content'>{para}</div><div class='newsletter'>{para}</div>"
                f"<div class='content'>{para * 2}</div></main>")
        self.assertEqual(clean_html(extract(page)).count("<p>"), 6)


class KOSyncTests(unittest.TestCase):
    def test_partial_md5_matches_koreader_algorithm(self):
        data = bytes(range(256)) * 400  # 102400 bytes
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            f.write(data)
        md5 = hashlib.md5()
        for offset in (0, 1024, 4096, 16384, 65536):  # 1024 << 2i below the file size
            md5.update(data[offset:offset + 1024])
        self.assertEqual(partial_md5(Path(f.name)), md5.hexdigest())
        Path(f.name).unlink()


if __name__ == "__main__":
    unittest.main()
