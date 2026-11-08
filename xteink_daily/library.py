"""Edition storage and the delivery ledger.

Layout:
    <library>/<project-id>/<Slug>-<YYYY-MM-DD>.epub        the edition (immutable once built)
    <library>/<project-id>/<Slug>-<YYYY-MM-DD>.epub.json   its metadata
    <library>/state.json                                   seen items + delivery ledger
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .config import Project

EDITION_RE = re.compile(r"^(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*?)-(?P<date>\d{4}-\d{2}-\d{2})\.epub$")

# Delivery targets recorded in the ledger.
TARGET_CROSSPOINT = "crosspoint"
TARGET_FOLDER = "folder"


@dataclass(frozen=True)
class Edition:
    project_id: str
    date: dt.date
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    @property
    def meta_path(self) -> Path:
        return self.path.with_name(self.path.name + ".json")

    def meta(self) -> Dict[str, Any]:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @property
    def title(self) -> str:
        return self.meta().get("title") or self.path.stem


class Library:
    _thread_lock = threading.Lock()

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = root / "state.json"
        self.lock_path = root / ".state.lock"

    # -- editions ---------------------------------------------------------

    def edition_path(self, project: Project, date: dt.date) -> Path:
        return self.root / project.id / f"{project.slug}-{date.isoformat()}.epub"

    def find(self, project: Project, date: dt.date) -> Optional[Edition]:
        path = self.edition_path(project, date)
        return Edition(project.id, date, path) if path.exists() else None

    def editions(self, project_ids: Optional[Sequence[str]] = None,
                 since: Optional[dt.date] = None) -> List[Edition]:
        out: List[Edition] = []
        if not self.root.exists():
            return out
        for pdir in sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if project_ids is not None and pdir.name not in project_ids:
                continue
            for f in pdir.glob("*.epub"):
                m = EDITION_RE.match(f.name)
                if not m:
                    continue
                try:
                    date = dt.date.fromisoformat(m.group("date"))
                except ValueError:
                    continue
                if since is None or date >= since:
                    out.append(Edition(pdir.name, date, f))
        out.sort(key=lambda e: (e.date, e.project_id))
        return out

    def get(self, project_id: str, filename: str) -> Optional[Edition]:
        if "/" in filename or "\\" in filename or not EDITION_RE.match(filename):
            return None
        for e in self.editions([project_id]):
            if e.filename == filename:
                return e
        return None

    # -- state ------------------------------------------------------------

    @contextlib.contextmanager
    def state(self) -> Iterator[Dict[str, Any]]:
        """Locked read-modify-write of state.json (safe across threads and processes)."""
        with self._thread_lock:
            with open(self.lock_path, "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    try:
                        st = json.loads(self.state_path.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        st = {}
                    st.setdefault("seen", {})
                    st.setdefault("delivered", {})
                    st.setdefault("devices", {})
                    before = copy.deepcopy(st)
                    yield st
                    if st != before:
                        tmp = self.state_path.with_name("state.json.tmp")
                        tmp.write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")
                        os.replace(tmp, self.state_path)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def delivered_names(st: Dict[str, Any], target: str) -> Dict[str, Any]:
        return st["delivered"].setdefault(target, {})

    @staticmethod
    def mark_delivered(st: Dict[str, Any], target: str, edition: Edition, via: str,
                       stored_as: Optional[str] = None) -> None:
        st["delivered"].setdefault(target, {})[edition.filename] = {
            "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "via": via,
            "as": stored_as or edition.filename,
            "date": edition.date.isoformat(),
        }

    def pending(self, st: Dict[str, Any], target: str, project_ids: Sequence[str],
                since: dt.date) -> List[Edition]:
        done = st["delivered"].get(target, {})
        return [e for e in self.editions(project_ids, since) if e.filename not in done]
