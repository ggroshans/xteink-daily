"""Generate (and optionally install) the CrossPoint SD-card plugin for sleep-pull delivery.

The plugin is a declarative device.json with one `sleep.enter` download handler:
each time the reader goes to sleep it joins Wi-Fi for a few seconds (battery
>= 20%, saved network) and asks this machine for the next undelivered edition.
See docs/plugin-events.md in the CrossPoint repository.
"""
from __future__ import annotations

import json
import tempfile
import urllib.parse
from pathlib import Path
from typing import Dict

from . import APP
from .config import Config, ConfigError
from .crosspoint import CrossPoint

PLUGIN_DIR_NAME = APP  # <= 23 bytes, required for event subscriptions
DEVICE_PLUGIN_ROOT = "/.crosspoint/plugins"


def render_plugin(cfg: Config) -> Dict[str, str]:
    base = cfg.server.public_url
    if not base:
        raise ConfigError("set server.public_url (e.g. http://192.168.1.20:8787) so the reader can reach this machine")
    if "localhost" in base or "127.0.0.1" in base:
        raise ConfigError("server.public_url must be this machine's LAN address, not localhost")
    url = f"{base}/crosspoint/next?ts={{event.ts}}&id={{event.id}}"
    if cfg.server.token:
        url += f"&token={urllib.parse.quote(cfg.server.token)}"
    folder = cfg.crosspoint.folder
    device = {
        "title": "Daily editions",
        "description": f"Fetches new editions into {folder} when the reader goes to sleep.",
        "events": {
            "sleep.enter": {
                "download": {"url": url, "dest": f"{folder}/Edition-{{event.ts}}.epub"},
                "toast": f"New edition saved to {folder}",
            }
        },
    }
    readme = f"""Daily editions ({APP})

Each time the reader goes to sleep it briefly joins your saved Wi-Fi network
and asks {base} for the next new edition. New editions are saved to
{folder} as Edition-<number>.epub; open them from the Library.

Nothing new? The request costs a few seconds of Wi-Fi and nothing is written.
The reader skips the check when the battery is under 20%.

To stop: delete this folder ({DEVICE_PLUGIN_ROOT}/{PLUGIN_DIR_NAME}) or turn
off Settings > Connectivity > Plugins.
"""
    return {"device.json": json.dumps(device, indent=2) + "\n", "README.md": readme}


def write_plugin(cfg: Config, out_dir: Path) -> Path:
    target = out_dir / PLUGIN_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    for name, content in render_plugin(cfg).items():
        (target / name).write_text(content, encoding="utf-8")
    return target


def install_plugin(cfg: Config, client: CrossPoint) -> str:
    remote = f"{DEVICE_PLUGIN_ROOT}/{PLUGIN_DIR_NAME}"
    client.ensure_dir(remote)
    with tempfile.TemporaryDirectory() as tmp:
        local = write_plugin(cfg, Path(tmp))
        for f in sorted(local.iterdir()):
            client.upload(f, remote, overwrite=True)
    return remote
