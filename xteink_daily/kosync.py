"""KOReader sync (KOSync) helpers: document hashes and progress lookups.

KOReader and CrossPoint both identify a book on the sync server by either:
  * binary:   MD5 over 1KB samples at offsets 0, 1K, 4K, 16K ... (KOReader's default), or
  * filename: MD5 of the file name.
Progress only syncs between devices when they compute the same hash, i.e. when
each device holds a byte-identical copy (binary) or the same file name (filename).
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .config import KOSyncConfig


def partial_md5(path: Path) -> str:
    step = size = 1024
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for i in range(-1, 11):
            f.seek(0 if i < 0 else step << (2 * i))
            chunk = f.read(size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def filename_md5(path: Path) -> str:
    return hashlib.md5(path.name.encode("utf-8")).hexdigest()


def get_progress(cfg: KOSyncConfig, document: str, timeout: float = 15) -> Optional[Dict[str, Any]]:
    """GET /syncs/progress/<document>. Returns None when the server has no record."""
    if not (cfg.username and cfg.password):
        raise ValueError("set koreader.kosync.username and password in config.json")
    url = cfg.server.rstrip("/") + "/syncs/progress/" + document
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.koreader.v1+json",
        "x-auth-user": cfg.username,
        "x-auth-key": hashlib.md5(cfg.password.encode("utf-8")).hexdigest(),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return None
            data = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise PermissionError("KOSync server rejected the username/password") from None
        raise
    return data if data.get("document") or data.get("percentage") is not None else None
