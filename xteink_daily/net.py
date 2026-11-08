"""Small HTTP GET helper for feeds and article pages."""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from email.message import Message
from typing import Tuple

from . import USER_AGENT

_contact = ""


def set_contact(value: str) -> None:
    """Contact info (URL or email) for the User-Agent. Wikimedia and others throttle
    anonymous-looking clients; config.json's "contact" sets this."""
    global _contact
    _contact = (value or "").strip()


def user_agent() -> str:
    return f"{USER_AGENT[:-1]}; {_contact})" if _contact else USER_AGENT


class TooLarge(ValueError):
    pass


RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT = 20.0


def _retry_delay(err: urllib.error.HTTPError, attempt: int) -> float:
    header = (err.headers or {}).get("Retry-After", "") if err.headers else ""
    if header.strip().isdigit():
        return min(float(header), MAX_RETRY_WAIT)
    return min(1.5 * (2 ** attempt), MAX_RETRY_WAIT)


def http_get(url: str, *, accept: str, max_bytes: int, timeout: float = 20.0,
             retries: int = 2) -> Tuple[bytes, Message]:
    """GET a URL, transparently decoding gzip/deflate (some servers compress unasked).
    Rate limits (429) and server errors (5xx) are retried with backoff, honoring Retry-After."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https", "file"):
        raise ValueError(f"unsupported URL scheme: {url}")
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent(), "Accept": accept, "Accept-Encoding": "gzip, deflate"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(max_bytes + 1)
                headers = resp.headers
            break
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == retries:
                raise
            time.sleep(_retry_delay(e, attempt))
    if len(raw) > max_bytes:
        raise TooLarge(f"{url}: larger than {max_bytes} bytes")
    encoding = (headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = _bounded(zlib.decompressobj(16 + zlib.MAX_WBITS), raw, max_bytes, url)
    elif encoding == "deflate":
        try:
            raw = _bounded(zlib.decompressobj(), raw, max_bytes, url)
        except zlib.error:  # some servers send raw deflate without the zlib header
            raw = _bounded(zlib.decompressobj(-zlib.MAX_WBITS), raw, max_bytes, url)
    return raw, headers


def _bounded(d, data: bytes, max_bytes: int, url: str) -> bytes:
    out = d.decompress(data, max_bytes + 1)
    if len(out) > max_bytes:
        raise TooLarge(f"{url}: decompresses to more than {max_bytes} bytes")
    return out
