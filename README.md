# xteink-daily

Builds daily EPUB "editions" from feeds and delivers them to an Xteink X4 running
[CrossPoint Reader](https://github.com/crosspoint-reader/crosspoint-reader) and to a KOReader device,
so reading progress syncs between the two through KOReader sync (KOSync).

Pure Python standard library (3.9+). Nothing to install.

```
feeds ──► build (05:30 daily) ──► library/<project>/<Title>-<YYYY-MM-DD>.epub   (immutable)
                                     │
             ┌───────────────────────┼─────────────────────────────┐
             ▼                       ▼                             ▼
   push via CrossPoint web     sleep-pull plugin on the X4     OPDS feed / copy_to folder
   server (/upload) whenever   (fetches on every sleep, no     for KOReader (and CrossPoint's
   the X4 is in File Transfer  buttons; optional)              own OPDS browser)
```

## Quick start

```sh
cd ~/projects/xteink-daily
bin/xteink-daily init          # writes config.json (already done here)
bin/xteink-daily build         # builds today's editions now
bin/xteink-daily status        # what exists and where it has been delivered
```

Then, on the X4: **Home → File Transfer → Join Network**, and:

```sh
bin/xteink-daily push          # finds the reader (crosspoint.local, cached IP, UDP discovery) and uploads
```

To make it daily and hands-off, run the daemon at login:

```sh
bin/xteink-daily launchd install   # builds at build_time, pushes when the X4 appears, serves OPDS + sleep-pull
tail -f library/logs/daemon.log
```

## Getting editions onto the X4

The CrossPoint web server **only runs while the reader is in File Transfer mode**, so a pure
"push" can only happen while you have that screen open. The daemon handles this by checking
every minute (only while something is waiting) and uploading the moment the reader shows up.
Editions you missed are back-filled (`backfill_days`). Editions you delete on the reader are not
re-sent. Our own editions older than `keep_days` are pruned. Nothing else on the card is ever touched.

**Optional, fully automatic:** the sleep-pull plugin. CrossPoint plugins can declare an HTTP
download that runs when the reader goes to sleep. The firmware joins your saved Wi-Fi for a few
seconds (only when battery ≥ 20%) and saves whatever this Mac returns. No File Transfer screen
and no buttons.

```sh
bin/xteink-daily plugin --install  # while the X4 is in File Transfer mode (or copy build/xteink-daily/ to /.crosspoint/plugins/)
```

Then on the reader: **Settings → Connectivity → Plugins → Enabled** and restart it (event
subscriptions load at boot). Notes:

- Files arrive as `/Daily/Edition-<timestamp>.epub`. The firmware can't put a date in the file
  name. Each edition still needs a unique name, because CrossPoint caches parsed books by path
  and would show stale pages if a file were replaced in place.
- Each sleep fetches at most one new edition. When there's nothing new the server answers `304`
  and nothing is written.
- Downloads are capped at 1 MB by the firmware. Text-only editions are ~60 KB, so this only
  matters if you add a very large project.
- The Mac must be awake and reachable at `server.public_url`. Give the Mac a DHCP reservation
  so that IP doesn't change, or re-run `plugin --install` after it does.

## KOReader sync between the two devices

KOSync syncs **reading position, not files**. Each device needs its own copy of the book, and
both must compute the same document ID. xteink-daily makes that work:

- Every device gets the **byte-identical** file (the same EPUB is pushed, served and copied).
- An edition is **never rebuilt once delivered** (`build --force` refuses), because changing
  the bytes would change its ID.
- `bin/xteink-daily hash <file>` prints both IDs KOReader/CrossPoint use, for checking.

Set up both devices with the **same server and account**:

| | CrossPoint (X4) | KOReader |
|---|---|---|
| Where | Settings → KOReader Sync | Tools (🔧) → Progress sync |
| Server | `https://sync.koreader.rocks`, server type **KOSync** | default (sync.koreader.rocks) |
| Account | same username/password (register once from either device) | same |
| Matching | **Binary** | Document matching method → **Binary** |
| When | Sync behavior **Smart**; set the Home button (or long-press) action to **Sync** for one-press sync while reading | enable **Auto sync** (pushes on close, pulls on open) |

Daily flow: read on KOReader, close the book (progress uploads), pick up the X4, open the same
edition, press **Sync**. Smart mode jumps to the furthest position without asking.

`bin/xteink-daily kosync` (after filling `koreader.kosync` in config.json) shows which device
last synced each recent edition and at what percentage, so you can confirm it's working.

(CrossPoint's own default server, `sync.crosspointreader.com`, also works if you point
KOReader's custom sync server at it. Use whichever you like, as long as it's the same on both.)

## Getting editions onto the KOReader device

KOReader has no push API, so pick one:

1. **OPDS** (works everywhere): in KOReader, 🔍 → OPDS catalog → add
   `http://<public_url>/opds` (for example `http://10.0.0.92:8787/opds`). New editions show at the top.
   Tap one to download. If `server.token` is set, use it as the password (any username).
   CrossPoint's own OPDS browser can use the same URL.
2. **A synced folder:** set `koreader.copy_to` to a folder that already syncs to the device
   (Syncthing, Dropbox, a mounted Kobo/Kindle). The daemon copies each new edition there and
   prunes old ones.

## Projects

Each entry in `projects` produces one edition per day. Today there's one source type, `rss`
(RSS 2.0, RSS 1.0 and Atom):

```json
{
  "id": "tech",                    // lowercase, used for folders/URLs
  "title": "Tech Digest",          // book title and file name: Tech-Digest-2026-11-08.epub
  "type": "rss",
  "max_age_hours": 30,             // ignore older items
  "max_articles": 30,              // per edition
  "feeds": [
    {"name": "Ars", "url": "https://feeds.arstechnica.com/arstechnica/index", "max_items": 8, "full_text": true}
  ]
}
```

- Items are never repeated across editions. Days with nothing new produce no edition.
- `full_text: true` fetches each article page and extracts the main text, for feeds that only
  carry summaries. It falls back to the summary when extraction looks wrong (paywalls,
  JavaScript-only pages).
- Images are dropped to keep files small and reader-friendly.

To add a new kind of content (weather, calendar, read-later, newsletters...), write a class with
`collect(project, seen, now, log) -> list[Section]` in `xteink_daily/sources/` and register it
in `SOURCES`. Building, delivery and sync all work unchanged.

## Commands

| Command | |
|---|---|
| `build [--project ID] [--force]` | build today's editions |
| `push [--host IP] [--dry-run] [--resend FILE…] [--no-prune]` | upload to the X4 now |
| `status [--probe]` | editions, deliveries, reader reachability |
| `run` | daemon: schedule + push-on-appear + server |
| `serve` | server only (OPDS, files, sleep-pull) |
| `plugin [--install]` | generate/install the sleep-pull plugin |
| `hash FILE…` / `kosync [FILE…]` | KOSync document IDs / progress lookup |
| `launchd install\|uninstall\|print` | run the daemon at login (macOS) |

Tests: `python3 -m unittest discover -s tests -t .` (includes a fake CrossPoint web server).

## Troubleshooting

- **Daemon can't reach the X4 but `push` from Terminal can:** macOS may require *Local Network*
  permission for `python3` when it runs from launchd. Check System Settings → Privacy & Security →
  Local Network.
- **Reader not found:** mDNS (`crosspoint.local`) doesn't work on every network. Pass the IP shown
  on the reader's screen with `--host`, or set `crosspoint.host`. Once found, its IP is remembered.
- **macOS firewall prompt** for incoming connections when the server starts: allow it, or the
  readers can't reach OPDS/sleep-pull.
- **Plugin does nothing:** plugins must be enabled and the reader restarted after install. The
  reader's clock must have been set once (its first Wi-Fi join does this). Battery must be ≥ 20%.
# xteink-daily
# xteink-daily
# xteink-daily
# xteink-daily
