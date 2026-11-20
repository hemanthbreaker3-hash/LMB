# Telegram upload performance notes (LMB)

## Target throughput

- **wzgram + MTProto + Hyper** path is always used when `USE_HYPER=True` (default).
- Fast upload starts at **full concurrency** (no slow 10→50 Mbps ramp).
- Design target on a good VPS/DC path: **~300 Mbps minimum class**, up to **~700 Mbps** when the network and Telegram DC allow it.
- No library can guarantee a fixed Mbps; real speed still depends on VPS uplink, DC route, FloodWaits, and helper accounts.

## Fast MTProto settings (current)

| Parameter | Value |
|-----------|--------|
| Part size | 512 KiB (Telegram max for big files) |
| Media sessions | **8** always (no size-based ramp) |
| Workers per session | **6** (48 workers ceiling) |
| Min file size for fast path | **1 MB** |
| `max_concurrent_transmissions` | **16** (env `WZGRAM_MAX_CONCURRENT`) |
| Override sessions | env `WZGRAM_FAST_UPLOAD_SESSIONS` (4–8) |

## Upload delivery path

1. **Primary hop (high speed):** user session string (`USER_SESSION_STRING`) uploads to the sudo-configured **`LEECH_LOG_CHAT`** (bot settings → Leech Log Chat).
2. **User DM:** bot copies from the leech chat to the requester’s private chat (**bot only**, no user session).
3. **Extra dumps:** bot also copies to user `LEECH_DUMP_CHATS`, owner `LEECH_DUMP_CHATS`, command `-up` destinations, and FFmpeg key dumps.

Configure in bot settings (sudo):

- `LEECH_LOG_CHAT` — primary archive chat
- `LEECH_DUMP_CHATS` — named additional dumps
- `USER_SESSION_STRING` — required for the user-session primary hop
- `HELPER_TOKENS` / `HELPER_STRINGS` — extra parallel clients for Hyper

## Dependencies

- Linux: `wzgram[fast]>=3.1.5` (fast extra / uvloop where supported).
- Windows: `wzgram>=3.1.5` without the Linux-only speed extra.
- Do not mix `wzgram` and older `wzxpyro` in the same environment.

## Download speed (max from first part)

Same philosophy as upload: full concurrency from the first request.

### Telegram Hyper download (`HypertgDownload`)
- Chunk size: **1 MiB** (Telegram GetFile maximum) from the first part
- Workers: high floor (4+), up to 16 / CPU×3 across helper clients
- Pipeline: starts at **max window** (no min→max ramp)
- Part size floor: 256 KiB so multi-client ranges engage early

### Aria2 / direct HTTP
- `split` / `max-connection-per-server`: **32**
- `min-split-size`: **512K**
- `disk-cache`: **512M**, `file-allocation=none`
- `optimize-concurrent-downloads=true`

### yt-dlp
- `concurrent_fragment_downloads`: **16**
- `http_chunk_size`: 10 MiB, larger buffer

Configure helpers with `HELPER_TOKENS` / `HELPER_STRINGS` and keep `USE_HYPER=True` for Telegram media downloads.
