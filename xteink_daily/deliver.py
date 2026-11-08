"""Deliver editions: push to CrossPoint over its web server, or copy into a folder."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import APP
from .config import Config
from .crosspoint import CrossPoint, CrossPointError, Unreachable, locate
from .library import EDITION_RE, TARGET_CROSSPOINT, TARGET_FOLDER, Edition, Library

# Files written by the sleep-pull plugin: <folder>/Edition-<unix ts>.epub
SLEEP_NAME_RE = re.compile(r"^Edition-(?P<ts>\d{9,11})\.epub$")


@dataclass
class PushReport:
    device: str
    version: str = ""
    uploaded: List[str] = field(default_factory=list)
    already_there: List[str] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{len(self.uploaded)} uploaded"]
        if self.already_there:
            parts.append(f"{len(self.already_there)} already on device")
        if self.pruned:
            parts.append(f"{len(self.pruned)} pruned")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return f"CrossPoint {self.device} (fw {self.version}): " + ", ".join(parts)


def _today() -> dt.date:
    return dt.datetime.now().astimezone().date()


def prunable(names: List[str], cfg: Config, today: dt.date, keep_days: int,
             ledger: Optional[Dict[str, dict]] = None) -> List[str]:
    """Names (in one folder) of our own editions older than keep_days. Never anything else."""
    if keep_days <= 0:
        return []
    cutoff = today - dt.timedelta(days=keep_days)
    slugs = {p.slug for p in cfg.projects}
    stored_as_date = {v.get("as"): v.get("date") for v in (ledger or {}).values()}
    out = []
    for name in names:
        m = EDITION_RE.match(name)
        if m and m.group("slug") in slugs:
            if dt.date.fromisoformat(m.group("date")) < cutoff:
                out.append(name)
            continue
        s = SLEEP_NAME_RE.match(name)
        if s:
            known = stored_as_date.get(name)
            date = (dt.date.fromisoformat(known) if known
                    else dt.datetime.fromtimestamp(int(s.group("ts"))).date())
            if date < cutoff:
                out.append(name)
    return out


def push_crosspoint(cfg: Config, library: Library, *, host: Optional[str] = None,
                    client: Optional[CrossPoint] = None, resend: Optional[List[str]] = None,
                    dry_run: bool = False, prune: bool = True,
                    log: Optional[logging.Logger] = None) -> Optional[PushReport]:
    """Upload undelivered editions to the reader. Returns None if no device answered."""
    log = log or logging.getLogger(APP)
    today = _today()
    cp_cfg = cfg.crosspoint
    project_ids = [p.id for p in cfg.enabled_projects]
    since = today - dt.timedelta(days=max(cp_cfg.backfill_days, 0))

    with library.state() as st:
        cached_ip = st["devices"].get(TARGET_CROSSPOINT, {}).get("ip")
        if resend:
            with_names = set(resend)
            pending = [e for e in library.editions(project_ids) if e.filename in with_names]
        else:
            pending = library.pending(st, TARGET_CROSSPOINT, project_ids, since)

    if client is None:
        found = locate(host or cp_cfg.host, cached_ip=None if host else cached_ip,
                       use_discovery=cp_cfg.discover and not host, log=log)
        if not found:
            return None
        client, status = found
    else:
        status = client.status()

    try:
        device_id = client.status(plugin=APP).get("deviceId", "")
    except (Unreachable, CrossPointError):
        device_id = ""
    report = PushReport(device=client.host, version=str(status.get("version", "?")))
    with library.state() as st:
        st["devices"][TARGET_CROSSPOINT] = {
            "ip": client.host, "version": report.version, "device": status.get("device", ""),
            "device_id": device_id,
            "last_seen": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    folder = cp_cfg.folder
    if dry_run:
        report.uploaded = [e.filename for e in pending]
        return report

    client.ensure_dir(folder)
    present = {f["name"] for f in client.list_dir(folder) if not f.get("isDirectory")}

    for ed in pending:
        if ed.filename in present and not resend:
            report.already_there.append(ed.filename)
            via = "present"
        else:
            try:
                client.upload(ed.path, folder, overwrite=bool(resend))
                report.uploaded.append(ed.filename)
                via = "push"
                log.info("uploaded %s -> %s", ed.filename, folder)
            except CrossPointError as e:
                if "already exists" in e.message:
                    report.already_there.append(ed.filename)
                    via = "present"
                else:
                    report.errors.append(f"{ed.filename}: {e}")
                    log.error("upload failed for %s: %s", ed.filename, e)
                    continue
            except Unreachable as e:
                report.errors.append(f"{ed.filename}: {e}")
                log.warning("device went away during upload: %s", e)
                break
        with library.state() as st:
            Library.mark_delivered(st, TARGET_CROSSPOINT, ed, via)

    if prune:
        with library.state() as st:
            ledger = dict(st["delivered"].get(TARGET_CROSSPOINT, {}))
        names = [n for n in present if n not in report.uploaded]
        old = prunable(names, cfg, today, cp_cfg.keep_days, ledger)
        if old:
            try:
                client.delete([f"{folder}/{n}" for n in old])
                report.pruned = old
                log.info("pruned %d old edition(s) from the reader", len(old))
            except (CrossPointError, Unreachable) as e:
                report.errors.append(f"prune: {e}")
    return report


def copy_to_folder(cfg: Config, library: Library, *, log: Optional[logging.Logger] = None) -> List[str]:
    """Copy new editions into koreader.copy_to (a Dropbox/Syncthing folder, a mounted reader...)."""
    log = log or logging.getLogger(APP)
    if not cfg.koreader.copy_to:
        return []
    dest = Path(cfg.koreader.copy_to)
    if not dest.is_dir():
        log.debug("copy_to folder %s not available; skipping", dest)
        return []
    today = _today()
    project_ids = [p.id for p in cfg.enabled_projects]
    since = today - dt.timedelta(days=max(cfg.crosspoint.backfill_days, 0))
    with library.state() as st:
        pending = library.pending(st, TARGET_FOLDER, project_ids, since)
    copied = []
    for ed in pending:
        target = dest / ed.filename
        if not target.exists():
            tmp = dest / f".{ed.filename}.tmp"
            shutil.copyfile(ed.path, tmp)
            os.replace(tmp, target)
            copied.append(ed.filename)
            log.info("copied %s -> %s", ed.filename, dest)
        with library.state() as st:
            Library.mark_delivered(st, TARGET_FOLDER, ed, "copy")
    for name in prunable([p.name for p in dest.iterdir() if p.is_file()], cfg, today,
                         cfg.koreader.keep_days):
        try:
            (dest / name).unlink()
            log.info("pruned %s from %s", name, dest)
        except OSError as e:
            log.warning("could not prune %s: %s", name, e)
    return copied
