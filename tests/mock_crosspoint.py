"""In-process fake of the CrossPoint web server, following docs/webserver-endpoints.md
and the error strings in src/network/CrossPointWebServer.cpp."""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Set


class MockCrossPoint:
    def __init__(self):
        self.files: Dict[str, bytes] = {}
        self.dirs: Set[str] = {"/", "/.crosspoint"}
        self.requests: List[str] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _join(parent: str, name: str) -> str:
        return (parent.rstrip("/") or "") + "/" + name

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/plain"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _form(self):
                n = int(self.headers.get("Content-Length", 0))
                return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                mock.requests.append(f"GET {url.path}")
                if url.path == "/api/status":
                    st = {"version": "1.2.0", "firmwareFamily": "crosspoint", "ip": mock.host,
                          "mode": "STA", "rssi": -50, "device": "X4"}
                    if "plugin" in q:
                        st["deviceId"] = "ab" * 32
                    return self._send(200, json.dumps(st), "application/json")
                if url.path == "/api/files":
                    path = q.get("path", "/")
                    out = []
                    for d in mock.dirs:
                        if d != "/" and d.rsplit("/", 1)[0] in (path.rstrip("/"),) and d != path:
                            name = d.rsplit("/", 1)[1]
                            if not name.startswith("."):
                                out.append({"name": name, "size": 0, "isDirectory": True, "isEpub": False})
                    for f, data in mock.files.items():
                        parent, name = f.rsplit("/", 1)
                        if (parent or "/") == path and not name.startswith("."):
                            out.append({"name": name, "size": len(data), "isDirectory": False,
                                        "isEpub": name.endswith(".epub")})
                    return self._send(200, json.dumps(out), "application/json")
                self._send(404, "Not found")

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                mock.requests.append(f"POST {url.path}")
                if url.path == "/mkdir":
                    f = self._form()
                    name, parent = f.get("name", ""), f.get("path", "/")
                    if name.startswith("."):
                        return self._send(403, "Cannot create protected item")
                    full = mock._join(parent, name)
                    if full in mock.dirs or full in mock.files:
                        return self._send(400, "Folder already exists")
                    if parent not in mock.dirs:
                        return self._send(500, "Failed to create folder")
                    mock.dirs.add(full)
                    return self._send(200, f"Folder created: {name}")
                if url.path == "/upload":
                    n = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(n)
                    boundary = self.headers["Content-Type"].split("boundary=", 1)[1].encode()
                    part = raw.split(b"--" + boundary)[1]
                    head, body = part.split(b"\r\n\r\n", 1)
                    body = body[: -2] if body.endswith(b"\r\n") else body
                    name = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                    path = q.get("path", "/")
                    full = mock._join(path, name)
                    if path not in mock.dirs:
                        return self._send(400, "Failed to create file")
                    if full in mock.files and q.get("overwrite") not in ("1", "true"):
                        return self._send(400, f"File already exists: {name}")
                    mock.files[full] = body
                    return self._send(200, f"File uploaded successfully: {name}")
                if url.path == "/delete":
                    f = self._form()
                    paths = json.loads(f["paths"]) if "paths" in f else [f["path"]]
                    for p in paths:
                        mock.files.pop(p, None)
                    return self._send(200, "Deleted")
                self._send(404, "Not found")

        return H
