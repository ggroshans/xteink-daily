"""Command line interface: python3 -m xteink_daily <command>."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import APP, __version__
from .build import build_project, build_tools
from .config import PROJECT_ROOT, Config, ConfigError, default_config_path, load_config
from .crosspoint import CrossPointError, Unreachable, locate
from .deliver import copy_to_folder, push_crosspoint
from .library import TARGET_CROSSPOINT, TARGET_FOLDER, Library
from .net import set_contact

LAUNCHD_LABEL = "com.xteink-daily"
log = logging.getLogger(APP)


def lan_ip() -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # no packets are sent for a UDP connect
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# -- commands ---------------------------------------------------------------

def _default_init_target() -> Path:
    # Running from a source checkout: keep config next to the code. Installed: ~/.config.
    if (PROJECT_ROOT / "pyproject.toml").exists() and os.access(PROJECT_ROOT, os.W_OK):
        return PROJECT_ROOT / "config.json"
    return Path.home() / ".config" / APP / "config.json"


def cmd_init(args) -> int:
    target = Path(args.config).expanduser() if args.config else _default_init_target()
    if target.exists():
        print(f"{target} already exists; edit it directly.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    example = json.loads((Path(__file__).parent / "config.example.json").read_text(encoding="utf-8"))
    ip = lan_ip()
    if ip:
        example["server"]["public_url"] = f"http://{ip}:{example['server'].get('port', 8787)}"
    target.write_text(json.dumps(example, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {target}")
    if ip:
        print(f"server.public_url set to http://{ip}:8787 (this Mac's current LAN address).")
    print("Next: edit the feeds, then run `bin/xteink-daily build` to make today's editions.")
    return 0


def cmd_build(args, cfg: Config) -> int:
    library = Library(cfg.library_dir)
    projects = [cfg.project(args.project)] if args.project else cfg.enabled_projects
    if not projects:
        print("No enabled projects in config.")
        return 1
    sources, llm = build_tools(cfg, library, log)
    failed = 0
    for project in projects:
        r = build_project(project, library, force=args.force, log=log, sources=sources, llm=llm, cfg=cfg)
        where = f" -> {r.edition.path}" if r.edition and r.status == "built" else ""
        print(f"{project.id}: {r.status}{' (' + r.message + ')' if r.message else ''}{where}")
        failed += r.status in ("failed", "locked")
    return 1 if failed else 0


def cmd_push(args, cfg: Config) -> int:
    library = Library(cfg.library_dir)
    report = push_crosspoint(cfg, library, host=args.host, resend=args.resend or None,
                             dry_run=args.dry_run, prune=not args.no_prune, log=log)
    if report is None:
        print("No CrossPoint reader found. On the reader: Home > File Transfer > Join Network, "
              "then run this again (or pass --host <ip shown on screen>).")
        return 2
    print(report.summary())
    for name in report.uploaded:
        print(f"  {'would upload' if args.dry_run else 'uploaded'} {name}")
    for name in report.pruned:
        print(f"  pruned {name}")
    for err in report.errors:
        print(f"  error: {err}")
    return 1 if report.errors else 0


def cmd_status(args, cfg: Config) -> int:
    library = Library(cfg.library_dir)
    today = dt.date.today()
    print(f"config:   {cfg.path}")
    print(f"library:  {cfg.library_dir}")
    print(f"build at: {cfg.build_time} daily")
    print(f"server:   {cfg.server.public_url or '(server.public_url not set)'}"
          f"{'' if cfg.server.enabled else ' [disabled]'}")
    with library.state() as st:
        delivered = {t: dict(v) for t, v in st["delivered"].items()}
        device = dict(st["devices"].get(TARGET_CROSSPOINT, {}))
    print("\nprojects:")
    for p in cfg.projects:
        print(f"  {p.id:<20} {p.type:<5} {'on ' if p.enabled else 'off'}  \"{p.title}\"")
    print(f"\neditions (last {args.days} days):   [X4 = on the CrossPoint reader, F = copied to folder]")
    eds = library.editions(since=today - dt.timedelta(days=args.days))
    if not eds:
        print("  none yet: run `bin/xteink-daily build`")
    for ed in reversed(eds):
        x4 = delivered.get(TARGET_CROSSPOINT, {}).get(ed.filename)
        folder = delivered.get(TARGET_FOLDER, {}).get(ed.filename)
        marks = ("X4" if x4 else "  ") + (" F" if folder else "  ")
        via = f" via {x4['via']}" + (f" as {x4['as']}" if x4.get("as") != ed.filename else "") if x4 else ""
        print(f"  {marks}  {ed.filename:<44} {ed.size // 1024:>5} KB{via}")
    print("\nreader:")
    if device:
        print(f"  last seen {device.get('last_seen')} at {device.get('ip')} "
              f"({device.get('device') or 'X4'}, firmware {device.get('version')})")
    found = locate(cfg.crosspoint.host, cached_ip=device.get("ip"), use_discovery=args.probe, log=log)
    if found:
        client, st = found
        print(f"  ONLINE now at {client.host} (mode {st.get('mode')}, rssi {st.get('rssi')} dBm)")
    else:
        print("  not reachable now (normal unless it is in File Transfer mode)")
    return 0


def cmd_sources(args, cfg: Config) -> int:
    library = Library(cfg.library_dir)
    sources, llm = build_tools(cfg, library, log)
    used = {p.type for p in cfg.projects}
    print(f"LLM: {llm.status}\n")
    for name in sorted(sources):
        obj = sources[name]
        desc = getattr(obj, "description", "") or (obj.__doc__ or "").strip().split("\n")[0]
        module = getattr(obj, "__module__", "")
        print(f"{name}{'  (used)' if name in used else ''}\n  {desc}\n  from {module}")
        for opt, help_text in (getattr(obj, "options_help", None) or {}).items():
            print(f"    {opt}: {help_text}")
        print()
    return 0


def cmd_serve(args, cfg: Config) -> int:
    from .server import App, make_server
    server = make_server(App(cfg, Library(cfg.library_dir), log), port=args.port)
    host, port = server.server_address[:2]
    print(f"Serving on http://{host}:{port}  (OPDS: {cfg.server.public_url or f'http://<this-mac>:{port}'}/opds)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_run(args, cfg: Config) -> int:
    from .daemon import Daemon
    Daemon(cfg, log, interval=args.interval).run(serve=not args.no_serve)
    return 0


def cmd_plugin(args, cfg: Config) -> int:
    from .plugin import DEVICE_PLUGIN_ROOT, PLUGIN_DIR_NAME, install_plugin, write_plugin
    if args.install:
        found = locate(args.host or cfg.crosspoint.host, use_discovery=not args.host, log=log)
        if not found:
            print("No CrossPoint reader found; put it in File Transfer mode first.")
            return 2
        client, _ = found
        remote = install_plugin(cfg, client)
        print(f"Installed plugin to {remote} on {client.host}.")
        print("On the reader: Settings > Connectivity > Plugins = Enabled, then restart the reader "
              "(event subscriptions load at boot).")
        return 0
    out = Path(args.out).expanduser() if args.out else PROJECT_ROOT / "build"
    target = write_plugin(cfg, out)
    print(f"Wrote {target}/ - copy that folder to {DEVICE_PLUGIN_ROOT}/{PLUGIN_DIR_NAME}/ on the SD card,")
    print("or run `bin/xteink-daily plugin --install` while the reader is in File Transfer mode.")
    return 0


def cmd_hash(args) -> int:
    from .kosync import filename_md5, partial_md5
    for f in args.files:
        p = Path(f)
        print(f"{p.name}\n  binary   {partial_md5(p)}\n  filename {filename_md5(p)}")
    return 0


def cmd_kosync(args, cfg: Config) -> int:
    from .kosync import filename_md5, get_progress, partial_md5
    library = Library(cfg.library_dir)
    files: List[Path] = [Path(f) for f in args.files]
    if not files:
        files = [e.path for e in library.editions(since=dt.date.today() - dt.timedelta(days=args.days))]
    for p in files:
        for method, digest in (("binary", partial_md5(p)), ("filename", filename_md5(p))):
            try:
                prog = get_progress(cfg.koreader.kosync, digest)
            except (PermissionError, ValueError) as e:
                print(f"error: {e}")
                return 1
            if prog:
                ts = dt.datetime.fromtimestamp(prog.get("timestamp", 0)).strftime("%a %d %b %H:%M")
                pct = float(prog.get("percentage") or 0) * 100
                print(f"{p.name}: {pct:.0f}% on '{prog.get('device')}' at {ts} ({method} match)")
                break
        else:
            print(f"{p.name}: no progress on the sync server yet")
    return 0


def _plist(cfg: Config) -> dict:
    logs = cfg.library_dir / "logs"
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, "-m", "xteink_daily", "--config", str(cfg.path), "run"],
        "EnvironmentVariables": {"PYTHONPATH": str(PROJECT_ROOT), "PYTHONUNBUFFERED": "1"},
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / "daemon.log"),
        "StandardErrorPath": str(logs / "daemon.log"),
    }


def cmd_systemd(args, cfg: Config) -> int:
    """Print a systemd user unit (Linux, e.g. a Raspberry Pi that stays on)."""
    print(f"""# Save as ~/.config/systemd/user/{APP}.service, then:
#   systemctl --user daemon-reload && systemctl --user enable --now {APP}
#   loginctl enable-linger $USER    # keep it running when you're logged out
[Unit]
Description={APP}: daily editions for CrossPoint and KOReader
After=network-online.target
Wants=network-online.target

[Service]
Environment=PYTHONPATH={PROJECT_ROOT}
Environment=PYTHONUNBUFFERED=1
ExecStart={sys.executable} -m xteink_daily --config {cfg.path} run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target""")
    return 0


def cmd_launchd(args, cfg: Config) -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if args.action == "print":
        sys.stdout.write(plistlib.dumps(_plist(cfg)).decode())
        return 0
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], capture_output=True)
    if args.action == "uninstall":
        if plist_path.exists():
            plist_path.unlink()
        print(f"Stopped and removed {LAUNCHD_LABEL}.")
        return 0
    (cfg.library_dir / "logs").mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(_plist(cfg)))
    r = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}")
        return 1
    print(f"Installed {plist_path} and started it. Log: {cfg.library_dir / 'logs' / 'daemon.log'}")
    return 0


# -- entry point ------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog=APP, description="Daily EPUB editions for CrossPoint (Xteink) and KOReader.")
    ap.add_argument("--config", help=f"path to config.json (default: {default_config_path()})")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"{APP} {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config.json from the example")

    p = sub.add_parser("build", help="build today's edition for each project")
    p.add_argument("--project", help="only this project id")
    p.add_argument("--force", action="store_true", help="rebuild today's edition if not yet delivered")

    p = sub.add_parser("push", help="upload new editions to the reader (File Transfer mode)")
    p.add_argument("--host", help="reader IP/hostname (default: config, then discovery)")
    p.add_argument("--resend", nargs="+", metavar="FILE", help="re-upload these edition filenames")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-prune", action="store_true", help="don't delete old editions from the reader")

    p = sub.add_parser("status", help="show editions, deliveries and reader reachability")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--probe", action="store_true", help="also try UDP discovery")

    sub.add_parser("sources", help="list available content source types and their options")

    p = sub.add_parser("serve", help="serve OPDS + sleep-pull endpoints only")
    p.add_argument("--port", type=int)

    p = sub.add_parser("run", help="daemon: build on schedule, push when the reader appears, serve")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between checks")
    p.add_argument("--no-serve", action="store_true")

    p = sub.add_parser("plugin", help="generate/install the CrossPoint sleep-pull plugin")
    p.add_argument("--out", help="write the plugin folder here (default: ./build)")
    p.add_argument("--install", action="store_true", help="upload it to the reader over Wi-Fi")
    p.add_argument("--host")

    p = sub.add_parser("hash", help="print KOReader sync document hashes for files")
    p.add_argument("files", nargs="+")

    p = sub.add_parser("kosync", help="look up reading progress on the KOSync server")
    p.add_argument("files", nargs="*", help="EPUB files (default: recent editions)")
    p.add_argument("--days", type=int, default=3)

    p = sub.add_parser("launchd", help="run the daemon at login on macOS")
    p.add_argument("action", choices=["install", "uninstall", "print"])

    sub.add_parser("systemd", help="print a systemd user unit for Linux")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "hash":
        return cmd_hash(args)
    try:
        cfg = load_config(Path(args.config) if args.config else None)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    set_contact(cfg.contact)
    handler = {
        "build": cmd_build, "push": cmd_push, "status": cmd_status, "serve": cmd_serve, "sources": cmd_sources,
        "run": cmd_run, "plugin": cmd_plugin, "kosync": cmd_kosync, "launchd": cmd_launchd,
        "systemd": cmd_systemd,
    }[args.cmd]
    try:
        return handler(args, cfg)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except (Unreachable, CrossPointError) as e:
        print(f"reader error: {e}", file=sys.stderr)
        return 1
