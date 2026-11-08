"""HTTP server for devices to pull editions.

    /                         small HTML index (handy from a phone)
    /opds                     OPDS 1.2 acquisition feed (KOReader, CrossPoint's OPDS browser)
    /editions/<project>/<f>   the EPUB files
    /crosspoint/next          sleep-pull endpoint used by the CrossPoint plugin
    /healthz                  liveness check

If server.token is set, requests need ?token=<token> or HTTP Basic auth with
the token as the password (any username), which is what OPDS clients send.
"""
from __future__ import annotations

import base64
import datetime as dt
import hmac
import html
import logging
import shutil
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from . import APP, __version__
from .build import SLEEP_DOWNLOAD_LIMIT
from .config import Config
from .deliver import SLEEP_NAME_RE
from .library import TARGET_CROSSPOINT, Edition, Library

OPDS_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
MIN_TRUSTED_TS = 1_600_000_000  # the reader sends ts=0 until its clock has been set


def _e(s: str) -> str:
    return html.escape(s or "", quote=True)


class App:
    def __init__(self, cfg: Config, library: Library, log: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.library = library
        self.log = log or logging.getLogger(APP)

    def recent(self, days: int = 14) -> List[Edition]:
        since = dt.date.today() - dt.timedelta(days=days)
        ids = [p.id for p in self.cfg.enabled_projects]
        return list(reversed(self.library.editions(ids, since)))


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{__version__}"
    app: App  # set on the subclass made in make_server()

    def log_message(self, fmt, *args):
        self.app.log.info("http %s %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------

    def _base_url(self) -> str:
        if self.app.cfg.server.public_url:
            return self.app.cfg.server.public_url
        return f"http://{self.headers.get('Host') or 'localhost'}"

    def _authorized(self, query) -> bool:
        token = self.app.cfg.server.token
        if not token:
            return True
        given = (query.get("token") or [""])[0]
        if given and hmac.compare_digest(given, token):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return hmac.compare_digest(pw, token)
        return False

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              extra: Optional[dict] = None) -> None:
        self.send_response(status)
        if status != HTTPStatus.NOT_MODIFIED:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, ed: Edition, stored_as: Optional[str] = None) -> bool:
        size = ed.size
        self.send_response(200)
        self.send_header("Content-Type", "application/epub+zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{stored_as or ed.filename}"')
        self.end_headers()
        if self.command == "HEAD":
            return True
        try:
            with open(ed.path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    # -- routes -----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, b"ok\n")
        if not self._authorized(query):
            return self._send(401, b"auth required\n",
                              extra={"WWW-Authenticate": f'Basic realm="{APP}"'})
        if path == "/":
            return self._index()
        if path == "/opds":
            return self._opds()
        if path == "/crosspoint/next":
            return self._crosspoint_next(query)
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "editions":
            ed = self.app.library.get(parts[1], urllib.parse.unquote(parts[2]))
            if ed:
                self._send_file(ed)
                return None
        return self._send(404, b"not found\n")

    def _index(self):
        rows = []
        for ed in self.app.recent():
            meta = ed.meta()
            href = f"/editions/{ed.project_id}/{urllib.parse.quote(ed.filename)}"
            rows.append(f'<li><a href="{_e(href)}">{_e(ed.title)}</a> '
                        f'<small>{meta.get("articles", "?")} articles · {ed.size // 1024} KB</small></li>')
        body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>{APP}</title><h1>{APP}</h1>"
                f"<p>OPDS catalog: <code>{_e(self._base_url())}/opds</code></p>"
                f"<ul>{''.join(rows) or '<li>No editions yet.</li>'}</ul>")
        self._send(200, body.encode(), "text/html; charset=utf-8")

    def _opds(self):
        base = self._base_url()
        token = self.app.cfg.server.token
        suffix = f"?token={urllib.parse.quote(token)}" if token else ""
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = []
        for ed in self.app.recent():
            meta = ed.meta()
            built = meta.get("built_at") or f"{ed.date.isoformat()}T00:00:00+00:00"
            try:
                updated = dt.datetime.fromisoformat(built).astimezone(dt.timezone.utc)
            except ValueError:
                updated = dt.datetime.combine(ed.date, dt.time(), dt.timezone.utc)
            href = f"{base}/editions/{ed.project_id}/{urllib.parse.quote(ed.filename)}{suffix}"
            entries.append(
                "<entry>"
                f"<title>{_e(ed.title)}</title>"
                f"<id>{_e(meta.get('identifier') or 'urn:' + APP + ':' + ed.filename)}</id>"
                f"<updated>{updated.strftime('%Y-%m-%dT%H:%M:%SZ')}</updated>"
                f"<author><name>{_e(APP)}</name></author>"
                f"<dc:issued>{ed.date.isoformat()}</dc:issued>"
                f"<dc:language>en</dc:language>"
                f"<content type=\"text\">{_e(meta.get('description', ''))}</content>"
                f'<link rel="http://opds-spec.org/acquisition" href="{_e(href)}" type="application/epub+zip"/>'
                "</entry>")
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" '
            'xmlns:opds="http://opds-spec.org/2010/catalog">'
            f"<id>urn:{APP}:catalog</id><title>Daily editions</title><updated>{now}</updated>"
            f"<author><name>{APP}</name></author>"
            f'<link rel="self" href="{_e(base)}/opds{_e(suffix)}" type="{OPDS_TYPE}"/>'
            f'<link rel="start" href="{_e(base)}/opds{_e(suffix)}" type="{OPDS_TYPE}"/>'
            + "".join(entries) + "</feed>\n")
        self._send(200, feed.encode("utf-8"), OPDS_TYPE + "; charset=utf-8")

    def _crosspoint_next(self, query):
        """Called by the reader's sleep.enter download handler.

        200 + EPUB => the reader saves it as <folder>/Edition-<ts>.epub.
        304        => nothing new; the reader keeps its files (non-2xx never overwrites).
        """
        ts = (query.get("ts") or [""])[0]
        if not ts.isdigit() or int(ts) < MIN_TRUSTED_TS:
            # Without a real timestamp the file name would collide; wait for a clock.
            return self._send(HTTPStatus.NOT_MODIFIED)
        stored_as = f"Edition-{ts}.epub"
        assert SLEEP_NAME_RE.match(stored_as)
        cfg = self.app.cfg
        since = dt.date.today() - dt.timedelta(days=max(cfg.crosspoint.backfill_days, 0))
        lib = self.app.library
        with lib.state() as st:
            ledger = st["delivered"].get(TARGET_CROSSPOINT, {})
            # The reader only repeats an event it did not record as delivered (e.g. the SD
            # write failed after our 200), so a repeat of the same ts gets the same edition again.
            retry = next((name for name, rec in ledger.items() if rec.get("as") == stored_as), None)
            if retry:
                ed_retry = next((e for e in lib.editions() if e.filename == retry), None)
                pending = [ed_retry] if ed_retry else []
            else:
                pending = [e for e in lib.pending(st, TARGET_CROSSPOINT, [p.id for p in cfg.enabled_projects], since)
                           if e.size <= SLEEP_DOWNLOAD_LIMIT]
        if not pending:
            return self._send(HTTPStatus.NOT_MODIFIED)
        ed = pending[0]
        if self._send_file(ed, stored_as) and self.command == "GET":
            with lib.state() as st:
                Library.mark_delivered(st, TARGET_CROSSPOINT, ed, "sleep", stored_as)
            self.app.log.info("sleep-pull: sent %s to the reader as %s", ed.filename, stored_as)
        return None


def make_server(app: App, bind: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((bind or app.cfg.server.bind, port if port is not None else app.cfg.server.port),
                                 handler)
    server.daemon_threads = True
    return server


def serve_in_background(app: App) -> ThreadingHTTPServer:
    server = make_server(app)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    return server
