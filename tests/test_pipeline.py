"""End-to-end: build from a local feed, push to a fake reader, sleep-pull and OPDS via the server."""
import base64
import datetime as dt
import email.utils
import json
import logging
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.mock_crosspoint import MockCrossPoint
from xteink_daily.build import build_project
from xteink_daily.config import load_config
from xteink_daily.crosspoint import CrossPoint
from xteink_daily.deliver import copy_to_folder, push_crosspoint
from xteink_daily.library import TARGET_CROSSPOINT, Library
from xteink_daily.plugin import install_plugin, render_plugin
from xteink_daily.server import App, make_server

log = logging.getLogger("test")
log.addHandler(logging.NullHandler())
log.propagate = False


def feed_xml(n: int, prefix: str) -> str:
    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    items = "".join(
        f"<item><title>{prefix} story {i}</title><link>https://example.com/{prefix}/{i}</link>"
        f"<guid>{prefix}-{i}</guid><pubDate>{now}</pubDate>"
        f"<description>&lt;p&gt;Body of story {i}, with &lt;b&gt;bold&lt;/b&gt; text.&lt;/p&gt;</description></item>"
        for i in range(n))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Local</title>{items}</channel></rss>'


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.feed = root / "feed.xml"
        self.feed.write_text(feed_xml(3, "a"), encoding="utf-8")
        self.copy_dir = root / "sync-folder"
        self.copy_dir.mkdir()
        cfg = {
            "library_dir": "library",
            "projects": [{"id": "morning", "title": "Morning Edition", "type": "rss",
                          "feeds": [{"url": self.feed.as_uri(), "name": "Local"}]}],
            "crosspoint": {"host": "unused.invalid", "folder": "/Daily", "keep_days": 7, "discover": False},
            "server": {"public_url": "http://192.168.1.20:8787", "token": "s3cret"},
            "koreader": {"copy_to": str(self.copy_dir)},
        }
        (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.cfg = load_config(root / "config.json")
        self.library = Library(self.cfg.library_dir)
        self.project = self.cfg.project("morning")
        self.mock = MockCrossPoint()

    def tearDown(self):
        self.mock.close()
        self.tmp.cleanup()

    def build(self, **kw):
        return build_project(self.project, self.library, log=log, **kw)

    def test_build_is_immutable_and_dedupes(self):
        r = self.build()
        self.assertEqual(r.status, "built", r.message)
        self.assertEqual(r.edition.filename, f"Morning-Edition-{dt.date.today()}.epub")
        self.assertEqual(r.edition.meta()["articles"], 3)
        self.assertEqual(self.build().status, "exists")
        # Same items tomorrow would be skipped as already seen.
        tomorrow = dt.datetime.now().astimezone() + dt.timedelta(days=1)
        self.assertEqual(self.build(now=tomorrow).status, "empty")

    def test_push_prune_and_ledger(self):
        edition = self.build().edition
        client = CrossPoint(self.mock.host)
        old = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        old_ts = int(time.time()) - 30 * 86400
        self.mock.dirs.add("/Daily")
        self.mock.files.update({
            f"/Daily/Morning-Edition-{old}.epub": b"old",     # ours, old -> pruned
            f"/Daily/Edition-{old_ts}.epub": b"old-sleep",     # ours (sleep-pull), old -> pruned
            f"/Daily/Some-Other-Book-{old}.epub": b"keep",     # not our slug -> kept
            "/Daily/MyNovel.epub": b"keep",                    # never touched
        })
        report = push_crosspoint(self.cfg, self.library, client=client, log=log)
        self.assertEqual(report.uploaded, [edition.filename])
        self.assertEqual(self.mock.files[f"/Daily/{edition.filename}"], edition.path.read_bytes())
        self.assertEqual(sorted(report.pruned), sorted([f"Morning-Edition-{old}.epub", f"Edition-{old_ts}.epub"]))
        self.assertIn("/Daily/MyNovel.epub", self.mock.files)
        self.assertIn(f"/Daily/Some-Other-Book-{old}.epub", self.mock.files)

        again = push_crosspoint(self.cfg, self.library, client=client, log=log)
        self.assertEqual(again.uploaded, [])
        # Deleting it on the reader does not make it come back.
        del self.mock.files[f"/Daily/{edition.filename}"]
        self.assertEqual(push_crosspoint(self.cfg, self.library, client=client, log=log).uploaded, [])
        # ...unless asked.
        self.assertEqual(push_crosspoint(self.cfg, self.library, client=client, log=log,
                                         resend=[edition.filename]).uploaded, [edition.filename])
        # A delivered edition can't be rebuilt (it would break KOReader sync).
        self.assertEqual(self.build(force=True).status, "locked")

    def test_push_creates_folder_and_handles_existing_file(self):
        edition = self.build().edition
        self.mock.dirs.add("/Daily")
        self.mock.files[f"/Daily/{edition.filename}"] = edition.path.read_bytes()
        report = push_crosspoint(self.cfg, self.library, client=CrossPoint(self.mock.host), log=log)
        self.assertEqual(report.already_there, [edition.filename])
        self.assertEqual(report.uploaded, [])

    def test_copy_to_folder(self):
        edition = self.build().edition
        self.assertEqual(copy_to_folder(self.cfg, self.library, log=log), [edition.filename])
        self.assertEqual((self.copy_dir / edition.filename).read_bytes(), edition.path.read_bytes())
        self.assertEqual(copy_to_folder(self.cfg, self.library, log=log), [])

    def test_server_opds_and_sleep_pull(self):
        edition = self.build().edition
        server = make_server(App(self.cfg, self.library, log), bind="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def get(path, auth=True):
            req = urllib.request.Request(base + path)
            if auth:
                req.add_header("Authorization", "Basic " + base64.b64encode(b"reader:s3cret").decode())
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        try:
            self.assertEqual(get("/opds", auth=False)[0], 401)
            status, body = get("/opds")
            self.assertEqual(status, 200)
            self.assertIn(f"/editions/morning/{edition.filename}?token=s3cret".encode(), body)
            status, body = get(f"/editions/morning/{edition.filename}")
            self.assertEqual((status, body), (200, edition.path.read_bytes()))
            self.assertEqual(get("/editions/morning/..%2Fstate.json")[0], 404)

            ts = int(time.time())
            self.assertEqual(get("/crosspoint/next?ts=0&token=s3cret", auth=False)[0], 304)
            status, body = get(f"/crosspoint/next?ts={ts}&token=s3cret", auth=False)
            self.assertEqual((status, body), (200, edition.path.read_bytes()))
            with self.library.state() as st:
                rec = st["delivered"][TARGET_CROSSPOINT][edition.filename]
            self.assertEqual((rec["via"], rec["as"]), ("sleep", f"Edition-{ts}.epub"))
            # A retried event (reader failed to save it) gets the same edition again...
            status, body = get(f"/crosspoint/next?ts={ts}&token=s3cret", auth=False)
            self.assertEqual((status, body), (200, edition.path.read_bytes()))
            # ...while a new sleep with nothing new gets 304.
            self.assertEqual(get(f"/crosspoint/next?ts={ts + 60}&token=s3cret", auth=False)[0], 304)
            # Push now knows the reader already has it.
            report = push_crosspoint(self.cfg, self.library, client=CrossPoint(self.mock.host), log=log)
            self.assertEqual(report.uploaded, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_plugin_render_and_install(self):
        files = render_plugin(self.cfg)
        device = json.loads(files["device.json"])
        handler = device["events"]["sleep.enter"]["download"]
        self.assertEqual(handler["dest"], "/Daily/Edition-{event.ts}.epub")
        self.assertTrue(handler["url"].startswith("http://192.168.1.20:8787/crosspoint/next?ts={event.ts}"))
        self.assertIn("token=s3cret", handler["url"])
        self.assertLess(len(files["device.json"]), 8192)
        remote = install_plugin(self.cfg, CrossPoint(self.mock.host))
        self.assertEqual(remote, "/.crosspoint/plugins/xteink-daily")
        self.assertIn(f"{remote}/device.json", self.mock.files)


if __name__ == "__main__":
    unittest.main()
