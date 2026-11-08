# xteink-daily

Fresh reading on your e-reader every morning. xteink-daily builds small daily EPUB "editions"
(news digests, flashcards, whatever a content source produces) and delivers them to an Xteink
X4/X3 running [CrossPoint Reader](https://github.com/crosspoint-reader/crosspoint-reader) and to
any device running [KOReader](https://koreader.rocks). Reading progress then syncs between the two
through KOReader sync (KOSync).

- **Zero dependencies:** Python 3.9+ standard library. Runs on a Mac, a Raspberry Pi or any Linux box.
- **Hands-off delivery to CrossPoint:** pushes over the reader's web server whenever it's in File
  Transfer mode, or (optionally) the reader pulls new editions by itself each time it goes to sleep.
- **KOReader:** an OPDS catalog, or copies into a folder you already sync to the device.
- **KOSync-safe:** every device gets the byte-identical file, so progress matches across devices.
- **Pluggable content:** RSS/Atom built in. Anything else is a small plugin or a script in any
  language. Optional LLM summaries via Claude.

```
content sources ──► build (daily) ──► library/<project>/<Title>-<YYYY-MM-DD>.epub   (immutable)
 rss, command,                          │
 content packs          ┌───────────────┼──────────────────────────┐
                        ▼               ▼                          ▼
              push to CrossPoint   sleep-pull plugin         OPDS catalog / synced folder
              (File Transfer mode)  on the reader (optional)  for KOReader
```

## Install

```sh
git clone https://github.com/ggroshans/xteink-daily.git
cd xteink-daily
bin/xteink-daily init        # writes config.json next to the code
```

Or install the command: `pipx install git+https://github.com/ggroshans/xteink-daily` (then
`xteink-daily init` writes `~/.config/xteink-daily/config.json`).

## Quick start

```sh
bin/xteink-daily build       # build today's editions from the projects in config.json
bin/xteink-daily status      # what exists and where it has been delivered
```

On the reader, open **Home → File Transfer → Join Network**, then:

```sh
bin/xteink-daily push        # finds the reader (crosspoint.local, cached IP, UDP discovery) and uploads
```

To run it every day without thinking about it, start the daemon. It builds at `build_time`,
pushes when the reader shows up and serves the OPDS and sleep-pull endpoints.

```sh
bin/xteink-daily launchd install    # macOS: runs at login; log in library/logs/daemon.log
bin/xteink-daily systemd            # Linux: prints a systemd user unit with install steps
```

## Content

Each entry in `projects` produces one edition per day, from the source named by `type`.
`xteink-daily sources` lists every installed source type with its options.

```json
"projects": [
  {"id": "morning", "title": "Morning Edition", "type": "rss", "max_age_hours": 30,
   "feeds": [{"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index",
              "max_items": 8, "full_text": true}]},
  {"id": "quote", "title": "Daily Quote", "type": "command", "command": ["python3", "quote.py"]}
]
```

- Items never repeat across editions. Days with nothing new produce no edition.
- **`rss`** handles RSS 2.0, RSS 1.0 and Atom. `full_text: true` fetches each article page and
  extracts the main text, for summary-only feeds.
- **`command`** runs any program that prints articles as JSON (see
  [docs/writing-sources.md](docs/writing-sources.md#command-sources-any-language)).
- **Content packs** add more types. [xteink-daily-content](https://github.com/ggroshans/xteink-daily-content)
  has a summarized Hacker News digest, Jeopardy! flashcards and vocabulary flashcards. Clone it and
  add to config.json:
  ```json
  "plugin_paths": ["../xteink-daily-content"],
  "plugins": ["xteink_daily_content"]
  ```
  or `pip install` it, and its sources register themselves.
- Items can have a **reveal page**: answers and definitions go on the next page, so you can quiz
  yourself by turning the page.

### LLM summaries (optional)

Sources can ask for summaries and explanations. With the
[Anthropic Python SDK](https://pypi.org/project/anthropic/) installed (`pip install anthropic`, or
`pip install "xteink-daily[llm]"`) and credentials available (`ANTHROPIC_API_KEY` or
`ant auth login`), they use Claude. Without it, sources fall back to non-LLM output (for example
the article's opening sentences), so nothing breaks. Settings live in the `llm` section:

```json
"llm": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "low", "fallbacks": true}
```

Responses are cached, so rebuilding a day doesn't pay twice. `effort` trades depth against cost.
`fallbacks` turns on Anthropic's server-side refusal fallback. Set `"provider": "none"` to turn
LLM use off.

### Writing a source

A source is a class with `collect(ctx)` that returns sections of articles. The `ctx` handles
HTTP, caching, article extraction, LLM calls, a daily-seeded random generator and "already
used" tracking. Keep your sources in your own repo or package; the core never needs to change.
See **[docs/writing-sources.md](docs/writing-sources.md)**.

## Getting editions onto CrossPoint

The CrossPoint web server only runs while the reader is in **File Transfer** mode. The daemon
checks for the reader every minute while something is waiting, and uploads as soon as it
appears. Missed editions are back-filled (`backfill_days`). Editions you delete on the reader
are not re-sent. Editions older than `keep_days` are pruned, and only ones xteink-daily created.

**Optional: fully automatic sleep-pull.** CrossPoint plugins can declare a download that runs
when the reader goes to sleep. The reader joins your saved Wi-Fi for a few seconds (battery ≥ 20%)
and fetches the next new edition from your xteink-daily machine. No File Transfer screen needed.

```sh
bin/xteink-daily plugin --install   # with the reader in File Transfer mode
```

Then on the reader: **Settings → Connectivity → Plugins → Enabled**, and restart it.

- Files arrive as `/Daily/Edition-<timestamp>.epub`. Each needs a unique name because CrossPoint
  caches parsed books by path.
- One new edition per sleep. When there's nothing new, the server answers `304` and nothing is written.
- The firmware caps these downloads at 1 MB. Text-only editions are typically 5–100 KB.
- The machine running xteink-daily must be on and reachable at `server.public_url`. Use a DHCP
  reservation so its address doesn't change.

## KOReader sync between devices

KOSync syncs **reading position, not files**: each device needs its own copy, and both must compute
the same document ID. xteink-daily delivers byte-identical files and never rebuilds an edition once
it has been delivered. `xteink-daily hash <file>` prints the IDs both readers use.

Use the **same server and account** on both:

| | CrossPoint | KOReader |
|---|---|---|
| Where | Settings → KOReader Sync | Tools → Progress sync |
| Server | `https://sync.koreader.rocks`, server type **KOSync** | default |
| Account | same username/password | same |
| Matching | **Binary** | Document matching method → **Binary** |
| Syncing | Sync behavior **Smart**; map the Home button (or long-press) to **Sync** | **Auto sync** on |

Read on KOReader, close the book, open the same edition on the X4 and press **Sync**.
`xteink-daily kosync` (with `koreader.kosync` credentials set) shows each edition's last synced
device and position.

## Getting editions onto KOReader

KOReader has no push API. Pick one:

1. **OPDS:** in KOReader, Search → OPDS catalog → add `http://<server.public_url>/opds`. New
   editions appear at the top; tap to download. CrossPoint's OPDS browser can use the same URL.
   If `server.token` is set, use it as the password.
2. **A synced folder:** set `koreader.copy_to` to a folder that already syncs to the device
   (Syncthing, Dropbox, a mounted Kobo/Kindle). New editions are copied in, old ones pruned.

## Commands

| Command | |
|---|---|
| `init` | create a config.json |
| `build [--project ID] [--force]` | build today's editions |
| `sources` | list source types, their options and the LLM status |
| `push [--host IP] [--dry-run] [--resend FILE…] [--no-prune]` | upload to the reader now |
| `status [--probe]` | editions, deliveries, reader reachability |
| `run` / `serve` | daemon (schedule + push + server) / server only |
| `plugin [--install]` | generate or install the sleep-pull plugin |
| `hash FILE…` / `kosync [FILE…]` | KOSync document IDs / progress lookup |
| `launchd install\|uninstall\|print` / `systemd` | run at login (macOS) / Linux unit |

## Troubleshooting

- **Reader not found:** mDNS (`crosspoint.local`) doesn't work on every network. Pass the IP shown
  on the reader with `--host`, or set `crosspoint.host`.
- **macOS: the daemon can't reach the reader but Terminal can:** grant *Local Network* access to
  `python3` (System Settings → Privacy & Security → Local Network). Allow incoming connections if
  the firewall asks, or readers can't reach OPDS/sleep-pull.
- **Sleep-pull does nothing:** plugins must be enabled and the reader restarted after install; its
  clock must have been set once (any Wi-Fi join does it); battery must be ≥ 20%.
- **HTTP 429 from Wikipedia or other APIs:** requests are retried with backoff. Setting `contact`
  in config.json (a URL or email) adds it to the User-Agent, which some services require.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The tests include a fake CrossPoint web server that follows the
[documented endpoints](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/webserver-endpoints.md).
Not affiliated with Xteink, CrossPoint Reader or KOReader.

MIT licensed.
