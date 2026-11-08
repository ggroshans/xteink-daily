"""Client for the CrossPoint Reader web server (File Transfer mode).

Endpoints: https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/webserver-endpoints.md
The server only runs while the reader is in File Transfer (or Calibre Wireless)
mode, so every call here must tolerate the device being absent.
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DISCOVERY_PORT = 8134

# Device traffic must never go through a system HTTP proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Unreachable(Exception):
    """The device did not answer (not in File Transfer mode, asleep, or off-network)."""


class CrossPointError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class CrossPoint:
    def __init__(self, host: str, timeout: float = 10.0):
        self.host = host
        self.base = f"http://{host}"
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"CrossPoint({self.host!r})"

    def _request(self, method: str, path: str, *, query: Optional[Dict[str, str]] = None,
                 data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
                 timeout: Optional[float] = None) -> bytes:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with _opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace").strip()
            raise CrossPointError(e.code, body or e.reason) from None
        except (urllib.error.URLError, OSError) as e:
            raise Unreachable(f"{self.host}: {getattr(e, 'reason', e)}") from None

    def _form(self, path: str, fields: Dict[str, str]) -> str:
        body = urllib.parse.urlencode(fields).encode()
        return self._request("POST", path, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded"}).decode("utf-8", errors="replace")

    # -- API --------------------------------------------------------------

    def status(self, plugin: Optional[str] = None) -> Dict[str, Any]:
        raw = self._request("GET", "/api/status", query={"plugin": plugin} if plugin else None)
        try:
            st = json.loads(raw)
        except ValueError:
            raise CrossPointError(200, "not a CrossPoint device (status is not JSON)") from None
        if not isinstance(st, dict) or "version" not in st:
            raise CrossPointError(200, "not a CrossPoint device (unexpected status payload)")
        return st

    def list_dir(self, path: str) -> List[Dict[str, Any]]:
        return json.loads(self._request("GET", "/api/files", query={"path": path}))

    def mkdir(self, parent: str, name: str) -> None:
        self._form("/mkdir", {"name": name, "path": parent})

    def ensure_dir(self, path: str) -> None:
        """Create each missing component of an absolute SD path."""
        current = "/"
        for part in [p for p in path.strip("/").split("/") if p]:
            # Dot-folders (e.g. /.crosspoint) can't be created over HTTP and are
            # hidden from listings; the firmware creates /.crosspoint itself.
            if not part.startswith("."):
                try:
                    self.mkdir(current, part)
                except CrossPointError as e:
                    if "already exists" not in e.message:
                        raise
            current = current.rstrip("/") + "/" + part

    def upload(self, local: Path, dest_dir: str, *, filename: Optional[str] = None,
               overwrite: bool = False) -> str:
        name = filename or local.name
        if any(c in name for c in '"/\\\r\n'):
            raise ValueError(f"unsafe upload filename: {name!r}")
        data = local.read_bytes()
        boundary = "----xteinkdaily" + secrets.token_hex(12)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        query = {"path": dest_dir}
        if overwrite:
            query["overwrite"] = "1"
        # The ESP32 writes to SD through a 4KB buffer; allow for slow cards.
        timeout = 30 + len(data) / 20_000
        return self._request("POST", "/upload", query=query, data=body, timeout=timeout, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }).decode("utf-8", errors="replace")

    def delete(self, paths: List[str]) -> str:
        if not paths:
            return ""
        return self._form("/delete", {"paths": json.dumps(paths)})


def discover(timeout: float = 1.5) -> List[Tuple[str, str]]:
    """UDP discovery: broadcast "hello" to port 8134, collect (ip, hostname) replies."""
    found: List[Tuple[str, str]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        sock.sendto(b"hello", ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (ip, _port) = sock.recvfrom(512)
            except socket.timeout:
                continue
            text = data.decode("utf-8", errors="replace")
            if text.startswith("crosspoint"):
                host = text.split("(on ", 1)[-1].split(")", 1)[0] if "(on " in text else ""
                if ip not in [f[0] for f in found]:
                    found.append((ip, host))
    except OSError:
        pass
    finally:
        sock.close()
    return found


def locate(host: str, *, cached_ip: Optional[str] = None, use_discovery: bool = True,
           log: Optional[logging.Logger] = None) -> Optional[Tuple[CrossPoint, Dict[str, Any]]]:
    """Find a reachable device. Returns (client pinned to its IP, status) or None."""
    log = log or logging.getLogger(__name__)
    candidates: List[str] = []
    for c in (cached_ip, host):
        if c and c not in candidates:
            candidates.append(c)

    def probe(addr: str):
        try:
            st = CrossPoint(addr, timeout=3).status()
        except (Unreachable, CrossPointError) as e:
            log.debug("no CrossPoint at %s: %s", addr, e)
            return None
        ip = st.get("ip") or addr
        return CrossPoint(ip), st

    for c in candidates:
        hit = probe(c)
        if hit:
            return hit
    if use_discovery:
        for ip, _name in discover():
            hit = probe(ip)
            if hit:
                return hit
    return None
