"""Load and validate config.json."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    pass


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-")
    return slug or "Edition"


@dataclass
class Project:
    id: str
    title: str
    type: str
    options: Dict[str, Any]
    enabled: bool = True

    @property
    def slug(self) -> str:
        return slugify(self.title)


@dataclass
class CrossPointConfig:
    host: str = "crosspoint.local"
    folder: str = "/Daily"
    keep_days: int = 7
    backfill_days: int = 2
    discover: bool = True


@dataclass
class ServerConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8787
    public_url: str = ""
    token: str = ""


@dataclass
class KOSyncConfig:
    server: str = "https://sync.koreader.rocks"
    username: str = ""
    password: str = ""


@dataclass
class KOReaderConfig:
    copy_to: str = ""
    keep_days: int = 7
    kosync: KOSyncConfig = field(default_factory=KOSyncConfig)


@dataclass
class LLMConfig:
    provider: str = "anthropic"      # "anthropic" (used only if the SDK + credentials exist) or "none"
    model: str = "claude-opus-5-5"
    effort: str = "low"              # low | medium | high | xhigh | max
    max_tokens: int = 16000
    fallbacks: bool = True           # server-side refusal fallback


@dataclass
class Config:
    path: Path
    library_dir: Path
    build_time: str
    projects: List[Project]
    crosspoint: CrossPointConfig
    server: ServerConfig
    koreader: KOReaderConfig
    llm: LLMConfig = field(default_factory=LLMConfig)
    contact: str = ""                                       # URL/email added to the User-Agent
    plugins: List[str] = field(default_factory=list)       # modules exposing SOURCES = {...}
    plugin_paths: List[Path] = field(default_factory=list)  # added to sys.path before importing them

    @property
    def enabled_projects(self) -> List[Project]:
        return [p for p in self.projects if p.enabled]

    def project(self, project_id: str) -> Project:
        for p in self.projects:
            if p.id == project_id:
                return p
        raise ConfigError(f"no project with id {project_id!r}")

    @property
    def build_hour_minute(self) -> tuple:
        m = TIME_RE.match(self.build_time)
        assert m
        return int(m.group(1)), int(m.group(2))


def default_config_path() -> Path:
    env = os.environ.get("XTEINK_DAILY_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (Path.cwd() / "config.json", PROJECT_ROOT / "config.json",
                      Path.home() / ".config" / "xteink-daily" / "config.json"):
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / "config.json"


def _strip_comments(obj: Any) -> Any:
    """Keys starting with "_" are comments (JSON has none of its own)."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _section(raw: Dict[str, Any], key: str, cls, **nested):
    data = dict(raw.get(key) or {})
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in {key!r}: {', '.join(sorted(unknown))}")
    for name, sub_cls in nested.items():
        data[name] = _section(data, name, sub_cls)
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"bad {key!r} section: {e}") from None


def load_config(path: Optional[Path] = None) -> Config:
    path = (path or default_config_path()).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"config not found: {path} (run `xteink-daily init` to create one)")
    try:
        raw = _strip_comments(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON: {e}") from None

    base = path.parent

    def resolve(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else (base / q).resolve()

    build_time = str(raw.get("build_time", "05:30"))
    if not TIME_RE.match(build_time):
        raise ConfigError(f"build_time must be HH:MM, got {build_time!r}")

    projects: List[Project] = []
    seen_ids = set()
    seen_slugs = set()
    for i, p in enumerate(raw.get("projects") or []):
        pid = p.get("id")
        if not isinstance(pid, str) or not PROJECT_ID_RE.match(pid):
            raise ConfigError(f"projects[{i}].id must match {PROJECT_ID_RE.pattern}")
        if pid in seen_ids:
            raise ConfigError(f"duplicate project id {pid!r}")
        title = p.get("title") or pid
        options = {k: v for k, v in p.items() if k not in ("id", "title", "type", "enabled")}
        proj = Project(id=pid, title=title, type=p.get("type", "rss"), options=options,
                       enabled=bool(p.get("enabled", True)))
        if proj.slug in seen_slugs:
            raise ConfigError(f"two projects share the filename slug {proj.slug!r}; give them distinct titles")
        seen_ids.add(pid)
        seen_slugs.add(proj.slug)
        projects.append(proj)

    cp = _section(raw, "crosspoint", CrossPointConfig)
    if not cp.folder.startswith("/") or ".." in cp.folder:
        raise ConfigError("crosspoint.folder must be an absolute SD-card path like /Daily")
    cp.folder = "/" + cp.folder.strip("/")

    server = _section(raw, "server", ServerConfig)
    server.public_url = server.public_url.rstrip("/")
    koreader = _section(raw, "koreader", KOReaderConfig, kosync=KOSyncConfig)
    if koreader.copy_to:
        koreader.copy_to = str(resolve(koreader.copy_to))
    llm = _section(raw, "llm", LLMConfig)
    if llm.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError(f"llm.effort must be low, medium, high, xhigh or max, got {llm.effort!r}")

    plugins = raw.get("plugins") or []
    plugin_paths = raw.get("plugin_paths") or []
    if not (isinstance(plugins, list) and all(isinstance(p, str) for p in plugins)):
        raise ConfigError("plugins must be a list of module names")
    if not (isinstance(plugin_paths, list) and all(isinstance(p, str) for p in plugin_paths)):
        raise ConfigError("plugin_paths must be a list of directories")

    return Config(
        path=path,
        library_dir=resolve(raw.get("library_dir", "library")),
        build_time=build_time,
        projects=projects,
        crosspoint=cp,
        server=server,
        koreader=koreader,
        llm=llm,
        contact=str(raw.get("contact", "")),
        plugins=plugins,
        plugin_paths=[resolve(p) for p in plugin_paths],
    )
