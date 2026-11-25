"""
Global FloodWait coordinator for high-concurrency bots (10k+ users).

Telegram rate-limits are per-account. When any client hits FloodWait we:
  1. Record a shared backoff so other tasks don't immediately re-trigger it.
  2. Apply a small jitter so retries don't stampede.
  3. Keep the event loop free (never block the main loop for long sleeps).

Used by upload/download paths and helper client start logic.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from typing import Optional

from ... import LOGGER

# Per-account (or "global") earliest-allowed timestamp
_backoff_until: dict[str, float] = defaultdict(float)
_lock = asyncio.Lock()

# Cap individual sleeps so the bot stays responsive even on heavy FloodWaits
MAX_SINGLE_SLEEP = 45.0
# Soft global pause after a severe flood (seconds)
SEVERE_THRESHOLD = 30


def _key(account: Optional[str] = None) -> str:
    return account or "global"


async def note_floodwait(seconds: float, account: Optional[str] = None) -> None:
    """Record that a FloodWait of `seconds` was received for this account."""
    try:
        wait = float(seconds)
    except (TypeError, ValueError):
        wait = 5.0
    # Jitter 5–15% so many clients don't wake at the exact same instant
    jitter = wait * random.uniform(0.05, 0.15)
    until = time.monotonic() + min(wait + jitter, MAX_SINGLE_SLEEP * 4)
    key = _key(account)
    async with _lock:
        # Only extend, never shorten an existing backoff
        if until > _backoff_until[key]:
            _backoff_until[key] = until
        if wait >= SEVERE_THRESHOLD:
            # Brief global soft-pause so the whole fleet cools down together
            g_until = time.monotonic() + min(wait * 0.25, 20.0)
            if g_until > _backoff_until["global"]:
                _backoff_until["global"] = g_until
    LOGGER.warning(
        "FloodWait noted account=%s wait=%.1fs (shared backoff until +%.1fs)",
        key,
        wait,
        until - time.monotonic(),
    )


async def wait_if_needed(account: Optional[str] = None) -> float:
    """
    Await any active shared backoff for this account (and global).
    Returns the number of seconds actually slept (0 if none).
    """
    key = _key(account)
    async with _lock:
        until = max(_backoff_until.get(key, 0.0), _backoff_until.get("global", 0.0))
    now = time.monotonic()
    if until <= now:
        return 0.0
    delay = min(until - now, MAX_SINGLE_SLEEP)
    # Tiny extra jitter
    delay += random.uniform(0.05, 0.4)
    await asyncio.sleep(delay)
    return delay


def remaining(account: Optional[str] = None) -> float:
    """Seconds remaining on shared backoff (0 if clear). Non-async snapshot."""
    key = _key(account)
    until = max(_backoff_until.get(key, 0.0), _backoff_until.get("global", 0.0))
    return max(0.0, until - time.monotonic())


async def run_with_flood_guard(coro_factory, *, account: Optional[str] = None, retries: int = 4):
    """
    Execute an async callable, retrying on FloodWait with shared backoff.
    `coro_factory` must be a zero-arg callable that returns a new awaitable each try.
    """
    from pyrogram.errors import FloodWait

    last_err = None
    for attempt in range(max(1, retries)):
        await wait_if_needed(account)
        try:
            return await coro_factory()
        except FloodWait as e:
            last_err = e
            await note_floodwait(getattr(e, "value", 5), account=account)
            continue
    if last_err:
        raise last_err
