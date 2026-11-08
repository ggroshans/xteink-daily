"""Plugin loading, the command source, reveal pages and the LLM wrapper."""
import datetime as dt
import json
import logging
import sys
import tempfile
import textwrap
import types
import unittest
import zipfile
from pathlib import Path

from xteink_daily.build import build_project, build_tools
from xteink_daily.config import LLMConfig, load_config
from xteink_daily.context import SourceContext
from xteink_daily.library import Library
from xteink_daily.llm import LLM

log = logging.getLogger("test-plugins")
log.addHandler(logging.NullHandler())
log.propagate = False

PLUGIN = '''
from xteink_daily.api import Article, Section, Source, text_to_html

class Riddle(Source):
    description = "A riddle with the answer on the next page."
    options_help = {"count": "how many"}

    def collect(self, ctx):
        n = int(ctx.option("count", 2))
        pool = [f"r{i}" for i in range(10) if f"r{i}" not in ctx.seen]
        ctx.rng.shuffle(pool)
        arts = [Article(uid=u, title=f"Riddle {u}", html=text_to_html("What has keys but no locks?"),
                        reveal=text_to_html("A piano."), byline="Puzzles") for u in pool[:n]]
        return [Section("Riddles", arts)]

SOURCES = {"riddle": Riddle}
'''

COMMAND = '''
import json, sys
req = json.load(sys.stdin)
print("hello from the script", file=sys.stderr)
if req["options"].get("fail"):
    sys.exit(3)
print(json.dumps({"name": "Quotes", "articles": [
    {"id": "q-" + req["date"], "title": "Quote for " + req["date"],
     "text": "Simplicity is prerequisite for reliability.", "reveal_text": "Edsger Dijkstra",
     "byline": "Daily quote"},
    {"id": "bad", "title": "<b>Bold</b> title", "html": "<p>unclosed <em>markup"}]}))
'''


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "pack").mkdir()
        (self.root / "pack" / "riddle_pack.py").write_text(PLUGIN)
        (self.root / "future_pack.py").write_text("REQUIRES_API = 99\nSOURCES = {'future': object}\n")
        (self.root / "quotes.py").write_text(COMMAND)
        cfg = {
            "plugins": ["riddle_pack", "future_pack"],
            "plugin_paths": ["pack", "."],
            "llm": {"provider": "none"},
            "projects": [
                {"id": "riddles", "title": "Riddle Me This", "type": "riddle", "count": 2},
                {"id": "quotes", "title": "Quotes", "type": "command", "command": [sys.executable, "quotes.py"]},
                {"id": "broken", "title": "Broken", "type": "command", "command": [sys.executable, "quotes.py"],
                 "fail": True},
            ],
        }
        (self.root / "config.json").write_text(json.dumps(cfg))
        self.cfg = load_config(self.root / "config.json")
        self.library = Library(self.cfg.library_dir)
        self.sources, self.llm = build_tools(self.cfg, self.library, log)

    def tearDown(self):
        for mod in ("riddle_pack", "future_pack"):
            sys.modules.pop(mod, None)
        for p in (str(self.root / "pack"), str(self.root)):
            while p in sys.path:
                sys.path.remove(p)
        self.tmp.cleanup()

    def build(self, pid, **kw):
        return build_project(self.cfg.project(pid), self.library, log=log, sources=self.sources,
                             llm=self.llm, cfg=self.cfg, **kw)

    def test_registry(self):
        self.assertIn("riddle", self.sources)
        self.assertIn("rss", self.sources)
        self.assertNotIn("future", self.sources, "plugins needing a newer API are skipped")

    def test_reveal_pages_follow_their_article_and_stay_out_of_the_toc(self):
        r = self.build("riddles")
        self.assertEqual(r.status, "built", r.message)
        with zipfile.ZipFile(r.edition.path) as z:
            names = z.namelist()
            spine = z.read("OEBPS/content.opf").decode()
            nav = z.read("OEBPS/nav.xhtml").decode()
            first = z.read("OEBPS/text/c001.xhtml").decode()
            reveal = z.read("OEBPS/text/c001r.xhtml").decode()
        self.assertIn("OEBPS/text/c001r.xhtml", names)
        self.assertLess(spine.index("text/c001.xhtml"), spine.index("text/c001r.xhtml"))
        self.assertLess(spine.index("text/c001r.xhtml"), spine.index("text/c002.xhtml"))
        self.assertNotIn("c001r", nav)
        self.assertIn("Puzzles", first)
        self.assertNotIn("piano", first)
        self.assertIn("A piano.", reveal)

    def test_seen_and_seeded_rng(self):
        first = self.build("riddles").edition.meta()
        tomorrow = dt.datetime.now().astimezone() + dt.timedelta(days=1)
        second = self.build("riddles", now=tomorrow)
        self.assertEqual(second.status, "built")
        with self.library.state() as st:
            self.assertEqual(len(st["seen"]["riddles"]), 4, "no riddle repeated across days")
        self.assertEqual(first["articles"], 2)

    def test_command_source(self):
        r = self.build("quotes")
        self.assertEqual(r.status, "built", r.message)
        with zipfile.ZipFile(r.edition.path) as z:
            c1 = z.read("OEBPS/text/c001.xhtml").decode()
            c1r = z.read("OEBPS/text/c001r.xhtml").decode()
            c2 = z.read("OEBPS/text/c002.xhtml").decode()
        self.assertIn("Simplicity is prerequisite", c1)
        self.assertIn("Daily quote", c1)
        self.assertIn("Edsger Dijkstra", c1r)
        self.assertIn("<h2>Bold title</h2>", c2)       # title markup stripped
        self.assertIn("<em>markup</em>", c2)          # body sanitized into well-formed XHTML

    def test_command_failure_fails_the_build(self):
        r = self.build("broken")
        self.assertEqual(r.status, "failed")
        self.assertIn("status 3", r.message)


# -- LLM wrapper against a fake anthropic SDK ---------------------------------

class _APIStatusError(Exception):
    def __init__(self, status_code, message="err"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def fake_sdk(behaviour):
    """behaviour(kind, kwargs) -> response object or raises."""
    mod = types.ModuleType("anthropic")
    mod.APIStatusError = _APIStatusError
    for name, base in (("AuthenticationError", _APIStatusError), ("PermissionDeniedError", _APIStatusError),
                       ("BadRequestError", _APIStatusError), ("NotFoundError", _APIStatusError),
                       ("RateLimitError", _APIStatusError)):
        setattr(mod, name, type(name, (base,), {}))
    mod.APIConnectionError = type("APIConnectionError", (Exception,), {})
    calls = []

    class _Messages:
        def __init__(self, kind):
            self.kind = kind

        def create(self, **kwargs):
            calls.append((self.kind, kwargs))
            return behaviour(self.kind, kwargs)

    class Anthropic:
        def __init__(self, **kw):
            self.messages = _Messages("messages")
            self.beta = types.SimpleNamespace(messages=_Messages("beta"))

    mod.Anthropic = Anthropic
    return mod, calls


def response(text, stop="end_turn"):
    return types.SimpleNamespace(stop_reason=stop, stop_details=None,
                                 content=[types.SimpleNamespace(type="thinking", thinking=""),
                                          types.SimpleNamespace(type="text", text=text)])


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self._saved = sys.modules.get("anthropic")

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._saved
        self.tmp.cleanup()

    def llm(self, **cfg):
        return LLM(LLMConfig(**cfg), self.cache, log)

    def test_request_shape_and_cache(self):
        sys.modules["anthropic"], calls = fake_sdk(lambda kind, kw: response("**Short** summary."))
        llm = self.llm()
        self.assertEqual(llm.ask("Summarize this"), "Short summary.")
        self.assertEqual(llm.ask("Summarize this"), "Short summary.")
        self.assertEqual(len(calls), 1, "second call served from cache")
        kind, kw = calls[0]
        self.assertEqual(kind, "beta")
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kw["fallbacks"], "default")
        self.assertEqual(kw["output_config"], {"effort": "low"})
        self.assertEqual(kw["messages"], [{"role": "user", "content": "Summarize this"}])

    def test_no_fallbacks_uses_plain_messages(self):
        sys.modules["anthropic"], calls = fake_sdk(lambda kind, kw: response("ok"))
        self.llm(fallbacks=False).ask("x")
        self.assertEqual(calls[0][0], "messages")
        self.assertNotIn("betas", calls[0][1])

    def test_refusal_returns_none(self):
        sys.modules["anthropic"], _ = fake_sdk(lambda kind, kw: response("", stop="refusal"))
        self.assertIsNone(self.llm().ask("x"))

    def test_bad_credentials_disable_for_the_run(self):
        def deny(kind, kw):
            raise sys.modules["anthropic"].AuthenticationError(401)
        sys.modules["anthropic"], calls = fake_sdk(deny)
        llm = self.llm()
        self.assertIsNone(llm.ask("a"))
        self.assertIsNone(llm.ask("b"))
        self.assertEqual(len(calls), 1)
        self.assertFalse(llm.available)

    def test_transient_errors_skip_one_item(self):
        state = {"n": 0}

        def flaky(kind, kw):
            state["n"] += 1
            if state["n"] == 1:
                raise sys.modules["anthropic"].RateLimitError(429)
            return response("fine")
        sys.modules["anthropic"], _ = fake_sdk(flaky)
        llm = self.llm()
        self.assertIsNone(llm.ask("a"))
        self.assertEqual(llm.ask("b"), "fine")

    def test_old_sdk_without_new_kwargs(self):
        def old(kind, kw):
            if "fallbacks" in kw or "output_config" in kw:
                raise TypeError("unexpected keyword argument 'fallbacks'")
            return response("old sdk ok")
        sys.modules["anthropic"], calls = fake_sdk(old)
        self.assertEqual(self.llm().ask("x"), "old sdk ok")
        self.assertEqual(calls[-1][1]["extra_body"], {"fallbacks": "default", "output_config": {"effort": "low"}})

    def test_missing_sdk_and_disabled_provider(self):
        sys.modules["anthropic"] = None  # makes `import anthropic` raise ImportError
        self.assertIsNone(self.llm().ask("x"))
        self.assertFalse(self.llm().available)
        self.assertIsNone(self.llm(provider="none").ask("x"))

    def test_context_summarize_falls_back_without_llm(self):
        sys.modules["anthropic"] = None
        ctx = SourceContext(project_id="p", title="P", source_type="t", options={},
                            now=dt.datetime.now().astimezone(), seen=frozenset(),
                            cache_root=self.cache, llm=self.llm(), log=log)
        text = "First sentence here. Second sentence follows. " + "Filler words go on. " * 50
        out = ctx.summarize(text, max_words=8)
        self.assertEqual(out, "First sentence here. Second sentence follows.")
        self.assertFalse(ctx.has_llm)


if __name__ == "__main__":
    unittest.main()
