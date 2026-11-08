# Writing content sources

A **source** turns some content (an API, a dataset, a website, your own notes) into the articles
of a daily edition. xteink-daily handles everything else: scheduling, the EPUB, skipping items
already used, delivery to CrossPoint and KOReader, and KOSync-safe files.

Sources live outside this repository, in a content pack (a folder or package of your own), so
adding ideas never touches the core. See
[xteink-daily-content](https://github.com/ggroshans/xteink-daily-content) for working examples.

There are two ways to write one:

- **A Python source** (below): a class with `collect(ctx)`. You get the helpers listed under
  "The context": HTTP, caching, article extraction and LLM calls.
- **A command source**: any program, in any language, that prints JSON. See
  [Command sources](#command-sources-any-language).

## A minimal Python source

```python
# my_pack/quotes.py
from xteink_daily.api import Article, Section, Source, text_to_html

class Quotes(Source):
    description = "One quote a day from a JSON feed."
    options_help = {"feed": "URL returning [{id, text, author}, ...]"}

    def collect(self, ctx):
        quotes = [q for q in ctx.get_json(ctx.option("feed")) if f"quote:{q['id']}" not in ctx.seen]
        if not quotes:
            return []
        q = ctx.rng.choice(quotes)
        return [Section("Quote", [Article(
            uid=f"quote:{q['id']}",
            title=q["author"],
            html=text_to_html(q["text"]),
        )])]
```

```python
# my_pack/__init__.py
from .quotes import Quotes

REQUIRES_API = 1
SOURCES = {"quotes": Quotes}
```

Then in config.json:

```json
"plugin_paths": ["~/code/my_pack_parent_folder"],
"plugins": ["my_pack"],
"projects": [{"id": "quote", "title": "Daily Quote", "type": "quotes", "feed": "https://..."}]
```

`xteink-daily sources` should now list `quotes`. Run `xteink-daily build --project quote`.

## Articles and sections

`collect(ctx)` returns a list of `Section(name, articles)`. Each section is a heading in the
table of contents. Each `Article` becomes one chapter:

| Field | |
|---|---|
| `uid` | **Required.** Stable id. Prefix it with your source name (`"vocab:ephemeral"`). Once an edition is built, its uids land in `ctx.seen` for later days. |
| `title` | **Required.** Plain-text chapter title. |
| `html` | **Required.** Body as an XHTML fragment. Pass anything you didn't write yourself through `clean_html()`; for plain text use `text_to_html()`. |
| `reveal` | Optional XHTML shown on the **next page**: answers, definitions, spoilers. |
| `byline` | Line under the title. `None` builds one from `source`/`author`/`published`; `""` hides it. |
| `link` | Original URL, shown as "Source: domain". |
| `source`, `author`, `published` | Used for the default byline (`published` is a timezone-aware `datetime`). |

Return an empty list (or empty sections) when there is nothing new. No edition is made that day.

Keep markup simple: `p`, `h3`–`h5`, `ul`/`ol`/`li`, `blockquote`, `em`, `strong`, `code`, `br`,
`hr`. E-ink readers (CrossPoint especially) use strict XML parsers with little memory. The builder
rejects malformed XHTML and falls back to plain text for that article. Images are not supported.

## The context

| | |
|---|---|
| `ctx.option(name, default)` | the project's settings from config.json |
| `ctx.path(value)` | resolve a path option (`~` and relative to config.json) |
| `ctx.date`, `ctx.now` | edition date, build time (timezone-aware) |
| `ctx.seen` | `frozenset` of uids used in earlier editions of this project (last 180 days) |
| `ctx.rng` | `random.Random` seeded with project + date: same picks if the day is rebuilt |
| `ctx.get(url)`, `ctx.get_text(url)`, `ctx.get_json(url)` | HTTP GET: gzip handled, size-capped, 429/5xx retried |
| `ctx.download(url, name, max_age_days=None)` | fetch once into `ctx.cache_dir` (datasets); returns the path |
| `ctx.article_html(url)` | main text of a web page as clean XHTML, or `None` |
| `ctx.summarize(text, instruction, max_words)` | LLM summary when available, otherwise the leading sentences |
| `ctx.ask(prompt, system=None)` | raw LLM call; returns `None` without an LLM |
| `ctx.has_llm` | whether an LLM is configured and working |
| `ctx.map(fn, items, workers=6)` | parallel map; an item that raises becomes `None` (and is logged) |
| `ctx.log` | a `logging.Logger` |

**Always have a non-LLM fallback.** Many users won't configure an LLM. `ctx.ask()` also returns
`None` on rate limits, refusals or network errors. LLM results are cached on disk.

**Be polite to free APIs.** Use `ctx.map(..., workers=2)` for services that throttle (Wikipedia
does), and cache datasets with `ctx.download()` rather than refetching them daily.

## Packaging a content pack

Without installing, a folder on `plugin_paths` and a module in `plugins` is enough (above). To
make `pip install` register your sources automatically, declare entry points:

```toml
# pyproject.toml
[project.entry-points."xteink_daily.sources"]
quotes = "my_pack.quotes:Quotes"
```

Set `REQUIRES_API` in your module. If a future xteink-daily changes the plugin API, older cores
skip packs that need a newer one, with a clear message, instead of crashing.

## Command sources (any language)

```json
{"id": "weather", "title": "Weather", "type": "command",
 "command": ["node", "weather.js"], "timeout": 120, "city": "Chicago"}
```

The program runs with the config file's folder as its working directory (override with `cwd`).
It receives one JSON object on **stdin**:

```json
{"api": 1, "project": "weather", "title": "Weather", "date": "2026-11-08",
 "now": "2026-11-08T05:30:00-06:00", "options": {"city": "Chicago", "...": "..."},
 "seen": ["uid", "..."]}
```

and prints one JSON object on **stdout**: either `{"sections": [{"name": ..., "articles": [...]}]}`
or, for a single section, `{"name": "...", "articles": [...]}`. Each article:

```json
{"id": "weather-2026-11-08", "title": "Chicago today",
 "text": "Plain text, blank lines between paragraphs"      // or "html": "<p>...</p>"
 , "reveal_text": "optional, shown on the next page"       // or "reveal_html"
 , "byline": "optional", "link": "https://...", "author": "...", "published": "ISO-8601"}
```

HTML is sanitized. Anything on stderr is logged. A non-zero exit fails that project's build
without affecting other projects.
