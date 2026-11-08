"""Optional LLM helper for sources (summaries, explanations).

Uses the official Anthropic Python SDK when it is installed (`pip install anthropic`,
or `pip install "xteink-daily[llm]"`) and credentials are available (ANTHROPIC_API_KEY
or an `ant auth login` profile). Without it, LLM.ask() returns None and sources fall
back to non-LLM output, so the core stays dependency-free.

Responses are cached on disk, so rebuilding or retrying a day never pays twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .config import LLMConfig

SYSTEM_DEFAULT = ("You write short pieces for a daily digest read on an e-ink e-reader. "
                  "Write plain prose with no markdown, headings, or preamble.")
MAX_INPUT_CHARS = 200_000


class LLM:
    def __init__(self, cfg: LLMConfig, cache_dir: Path, log: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.log = log or logging.getLogger(__name__)
        self._client = None
        self._sdk = None
        self._lock = threading.Lock()
        self._disabled_reason: Optional[str] = None
        if cfg.provider == "none":
            self._disabled_reason = "disabled in config (llm.provider = none)"
        elif cfg.provider != "anthropic":
            self._disabled_reason = f"unknown llm.provider {cfg.provider!r}"

    # -- setup ------------------------------------------------------------

    def _ensure_client(self) -> bool:
        if self._disabled_reason:
            return False
        if self._client is not None:
            return True
        with self._lock:
            if self._client is not None or self._disabled_reason:
                return self._client is not None
            try:
                import anthropic  # optional dependency
            except ImportError:
                self._disable("the anthropic package is not installed (pip install anthropic)")
                return False
            try:
                # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
                self._client = anthropic.Anthropic(max_retries=3, timeout=180.0)
            except Exception as e:  # no credentials, bad profile...
                self._disable(f"could not create an Anthropic client: {e}")
                return False
            self._sdk = anthropic
            return True

    def _disable(self, reason: str) -> None:
        if not self._disabled_reason:
            self._disabled_reason = reason
            self.log.warning("LLM features off: %s; sources will use non-LLM fallbacks", reason)

    @property
    def available(self) -> bool:
        return self._ensure_client()

    @property
    def status(self) -> str:
        if self._ensure_client():
            return f"anthropic / {self.cfg.model} (effort {self.cfg.effort})"
        return f"off: {self._disabled_reason}"

    # -- cache ------------------------------------------------------------

    def _cache_path(self, system: str, prompt: str) -> Path:
        key = hashlib.sha256(json.dumps([self.cfg.model, self.cfg.effort, system, prompt]).encode()).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.txt"

    # -- calls ------------------------------------------------------------

    def ask(self, prompt: str, system: Optional[str] = None) -> Optional[str]:
        """One-shot request. Returns the text, or None if unavailable/failed/refused."""
        system = system or SYSTEM_DEFAULT
        if len(prompt) > MAX_INPUT_CHARS:
            self.log.warning("LLM prompt is %d chars; trimming to %d", len(prompt), MAX_INPUT_CHARS)
            prompt = prompt[:MAX_INPUT_CHARS]
        cached = self._cache_path(system, prompt)
        if cached.exists():
            return cached.read_text(encoding="utf-8")
        if not self._ensure_client():
            return None
        text = self._call(system, prompt)
        if text:
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_name(cached.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, cached)
        return text

    def _call(self, system: str, prompt: str) -> Optional[str]:
        sdk, client = self._sdk, self._client
        params: Dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": self.cfg.effort},
        }
        try:
            if self.cfg.fallbacks:
                # Server-side refusal fallback: a declined request is re-run on Anthropic's
                # recommended model for that refusal category, inside the same call.
                try:
                    resp = client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
                except TypeError:
                    # Older SDK releases don't know these keyword arguments yet.
                    extra = {"fallbacks": "default", "output_config": params.pop("output_config")}
                    resp = client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], extra_body=extra, **params)
            else:
                try:
                    resp = client.messages.create(**params)
                except TypeError:
                    extra = {"output_config": params.pop("output_config")}
                    resp = client.messages.create(extra_body=extra, **params)
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as e:
            self._disable(f"credentials rejected ({e.status_code})")
            return None
        except (sdk.BadRequestError, sdk.NotFoundError) as e:
            # Usually configuration (unknown model, invalid parameter): stop trying this run.
            self._disable(f"request rejected ({e.status_code}): {getattr(e, 'message', e)}")
            return None
        except sdk.RateLimitError:
            self.log.warning("LLM rate limited after retries; using fallback for this item")
            return None
        except sdk.APIStatusError as e:
            self.log.warning("LLM API error %s; using fallback for this item", e.status_code)
            return None
        except sdk.APIConnectionError as e:
            self.log.warning("LLM connection failed (%s); using fallback for this item", e)
            return None

        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            self.log.info("LLM declined an item (category %s); using fallback", category)
            return None
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        if resp.stop_reason == "max_tokens":
            self.log.warning("LLM output hit max_tokens (%d); raise llm.max_tokens if this repeats",
                             self.cfg.max_tokens)
        return _strip_markdown(text) or None


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    return text.strip()


def extractive_summary(text: str, max_words: int) -> str:
    """Non-LLM fallback: leading sentences up to max_words."""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    out, count = [], 0
    for s in sentences:
        words = len(s.split())
        if out and count + words > max_words:
            break
        out.append(s)
        count += words
    summary = " ".join(out)
    if len(summary.split()) > max_words * 1.5:
        summary = " ".join(summary.split()[:max_words]) + "…"
    return summary
