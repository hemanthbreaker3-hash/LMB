# LMB — 10k+ concurrent users (cbgram performance)

Built on **cbgram** + **cbcrypto** Rust core: https://cbgram.pages.dev/

## Required environment (production)

```bash
export CBGRAM_RUST_CORE=1          # Rust AES + file engine (mandatory for max speed)
export CBGRAM_UVLOOP=1             # uvloop event loop (Linux/macOS)
export CBGRAM_MAX_CONCURRENT=1024  # concurrent MTProto transmissions per client
export CBGRAM_WORKERS=256          # client worker pool
```

Install:

```bash
pip install -U "cbgram[fast]" cbcrypto
# Do NOT mix with pyrogram / kurigram / pyrofork / wzxpyro
```

## Equal speed for every user

- Helper bots (`HELPER_TOKENS`) and helper users (`HELPER_STRINGS`) are load-balanced with least-loaded selection (`TgClient.pick_least_loaded`).
- All users share the same concurrency pool — no priority starvation.
- Primary high-speed hop uses `USER_SESSION_STRING` → leech log chat, then bot copies to user DM.

## FloodWait resilience

- Shared backoff coordinator (`bot/helper/ext_utils/flood_guard.py`) prevents stampede retries.
- `sleep_threshold=30` on clients; longer waits use the global guard.
- Status edit interval default raised to **8s** to reduce edit floods under load.

## Operational tips for 10k+

1. **Many helper accounts** — add as many `HELPER_TOKENS` / `HELPER_STRINGS` as you can (different accounts = different rate limits).
2. **Proxies** — optional `HELPER_BOT_PROXIES` / `HELPER_USER_PROXIES` to spread DC load.
3. **VPS** — prefer high single-core + good uplink to Telegram DCs; RAM scales with concurrent tasks.
4. **Queues** — leave `QUEUE_*` / `BOT_MAX_TASKS` / `USER_MAX_TASKS` at `0` (unlimited) unless you need hard caps.
5. **Status** — users should use `/status` sparingly; TMV auto-leech uses `/tstatus` / `/ts` separately.

## What this stack already does

| Path | Behaviour |
|------|-----------|
| Upload | cbgram Rust engine, full concurrency from first part, multi media sessions |
| Download (Telegram) | Hyper multi-client ranges, 1 MiB chunks |
| Aria2 / direct | 32 connections, 512K min-split |
| yt-dlp | 16 concurrent fragments |

Real Mbps still depends on VPS uplink, Telegram DC, and FloodWaits. No library can hard-guarantee a fixed speed.
