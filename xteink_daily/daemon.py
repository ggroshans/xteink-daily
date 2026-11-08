"""Long-running loop: build editions on schedule, push to the reader when it shows up, serve pulls."""
from __future__ import annotations

import datetime as dt
import logging
import signal
import threading
import time
from typing import Dict, Tuple

from .build import build_project, build_tools
from .config import Config
from .deliver import copy_to_folder, push_crosspoint
from .library import TARGET_CROSSPOINT, Library
from .server import App, serve_in_background

RETRY_EMPTY_MINUTES = 90
MAX_ATTEMPTS_PER_DAY = 6


class Daemon:
    def __init__(self, cfg: Config, log: logging.Logger, interval: float = 60.0):
        self.cfg = cfg
        self.log = log
        self.interval = interval
        self.library = Library(cfg.library_dir)
        self.sources, self.llm = build_tools(cfg, self.library, log)
        self.attempts: Dict[str, Tuple[dt.date, int, float]] = {}
        self.stop = threading.Event()

    def maybe_build(self, now: dt.datetime) -> None:
        hh, mm = self.cfg.build_hour_minute
        if (now.hour, now.minute) < (hh, mm):
            return
        today = now.date()
        for project in self.cfg.enabled_projects:
            if self.library.find(project, today):
                continue
            day, count, last = self.attempts.get(project.id, (today, 0, 0.0))
            if day != today:
                count, last = 0, 0.0
            if count >= MAX_ATTEMPTS_PER_DAY or time.time() - last < RETRY_EMPTY_MINUTES * 60:
                continue
            self.attempts[project.id] = (today, count + 1, time.time())
            result = build_project(project, self.library, now=now, log=self.log,
                                   sources=self.sources, llm=self.llm, cfg=self.cfg)
            self.log.info("build %s: %s %s", project.id, result.status, result.message)

    def maybe_push(self) -> None:
        today = dt.datetime.now().astimezone().date()
        since = today - dt.timedelta(days=max(self.cfg.crosspoint.backfill_days, 0))
        with self.library.state() as st:
            pending = self.library.pending(st, TARGET_CROSSPOINT, [p.id for p in self.cfg.enabled_projects], since)
        if not pending:
            return
        report = push_crosspoint(self.cfg, self.library, log=self.log)
        if report is None:
            self.log.debug("%d edition(s) waiting; reader not in File Transfer mode", len(pending))
        else:
            self.log.info(report.summary())

    def tick(self) -> None:
        now = dt.datetime.now().astimezone()
        for step in (lambda: self.maybe_build(now),
                     lambda: copy_to_folder(self.cfg, self.library, log=self.log),
                     self.maybe_push):
            try:
                step()
            except Exception:
                self.log.exception("daemon step failed")

    def run(self, serve: bool = True) -> None:
        if serve and self.cfg.server.enabled:
            srv = serve_in_background(App(self.cfg, self.library, self.log))
            self.log.info("serving on %s:%d (devices use %s)", *srv.server_address[:2],
                          self.cfg.server.public_url or "server.public_url unset")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        self.log.info("daemon started; build time %s, checking every %ds", self.cfg.build_time, self.interval)
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(self.interval)
        self.log.info("daemon stopped")
