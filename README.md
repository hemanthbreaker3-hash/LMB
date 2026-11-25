# 💎 LMB — High-Speed Telegram Mirror & Leech Bot

**LMB** is a high-performance Telegram Mirror & Leech Bot built for Linux VPS and Docker deployments. Powered by **`cbgram`** + **`cbcrypto`** (compiled Rust MTProto crypto and transfer engine with `uvloop`), LMB delivers instant, full-speed Telegram uploads, parallel multi-tasking, media processing, and automated 1TamilMV monitoring.

---

## ⚡ Core Highlights

- **Ultra-Fast `cbgram` + `cbcrypto` Engine:** Uses `cbgram[fast]` and `cbcrypto` with `CBGRAM_RUST_CORE=1`, `CBGRAM_UVLOOP=1`, `CBGRAM_MAX_CONCURRENT=256`, `CBGRAM_WORKERS=128`, and `max_parallel=16–64` for immediate peak throughput from the first tick.
- **Smart Telegram Destination & Dump Routing:**
  - Primary uploads use `USER_SESSION_STRING` to upload directly to `LEECH_LOG_CHAT` / `LEECH_DUMP_CHATS` (supports up to 4 GiB with Telegram Premium).
  - User DM delivery and additional dump fan-out always use `TgClient.bot` only (never user session).
- **1TamilMV Auto-Leech (`/tmv`) & Quality Picker (`-tmv`):**
  - `/tmv` (with `CMD_SUFFIX`) in DM lets every user manage custom/comma-separated domains, **Pause/Resume** monitoring (incoming releases only), **Independent** vs **User Settings** modes, **Upload Destination** (`Telegram`, `Mirror`, `Uphoster`), and unlimited keyword-based **Dump Rules** (`name | chat_id`, `.` for all files).
  - `<topic_url> -tmv` opens a paginated quality picker with **Magnet**, **Torrent** (both via qBittorrent), and **Cyberloom** filter tabs, cross-tab multi-select, and full flag support (`-e`, `-z`, `-d`, `-doc`, `-med`, `-n`, `-m`, `-up`, `-t`, etc.).
- **Built-in `/gensession`:** Step-by-step `cbgram` session string generator in DM for admins (`API ID` → `API Hash` → `+countrycode` phone → `OTP` → `2FA`).
- **DDL / Uphoster Support:** Fixed integrations for **GoFile**, **BuzzHeavier**, **PixelDrain**, **DevUploads**, **VikingFile**, and custom HTTP APIs, plus `/go?t=` pop-up redirect protection.
- **Telegram Premium Emojis & Arrow-Flow UI:** Automatic BotFather/custom emoji pack syncing (`emoji_utils.py`) and clean `━━━━━━━➤────` arrow-flow progress bars.

---

## 🚀 Quick VPS Deployment

Export your credentials in the shell (shell exports always take top priority over `config.py` and MongoDB), then run:

```bash
export BOT_TOKEN="123456:ABC..."
export OWNER_ID="123456789"
export TELEGRAM_API="1234567"
export TELEGRAM_HASH="your_api_hash"
export USER_SESSION_STRING="your_cbgram_session"   # optional, for >2 GiB & fast dump uploads
export DATABASE_URL="mongodb+srv://..."            # optional, for persistent settings

chmod +x deploy.vps
./deploy.vps
```

### Useful Service Commands

```bash
systemctl status lmb_bot --no-pager -l
journalctl -u lmb_bot -f
```

---

## ⚙️ Key Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CBGRAM_RUST_CORE` | `1` | Enable compiled Rust `cbcrypto` transfer & crypto engine |
| `CBGRAM_UVLOOP` | `1` | Enable `uvloop` event loop on Linux/macOS |
| `CBGRAM_MAX_CONCURRENT` | `256` | Max concurrent MTProto transmissions per client |
| `CBGRAM_WORKERS` | `128` | Client worker pool size |
| `BOT_THREAD_POOL_SIZE` | `128` | `ThreadPoolExecutor` workers for media/sync tasks |
| `USE_PREMIUM_EMOJIS` | `True` | Automatically render synced BotFather/custom `<tg-emoji>` |
| `PREMIUM_EMOJI_PACK` | `""` | Optional custom emoji pack short name(s) or `t.me/addemoji/...` |
| `CMD_SUFFIX` | `""` | Command suffix (e.g. `1` → `/leech1`, `/tmv1`, `/gensession1`) |
