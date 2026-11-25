# 💎 LMB — High-Speed Telegram Mirror & Leech Bot

**LMB** is a high-speed Telegram Mirror & Leech Bot built for Linux VPS and Docker deployments. Powered by **`cbgram` `0.3.1`** + **`cbcrypto`** (compiled Rust MTProto crypto and transfer engine with `uvloop`), LMB delivers peak Telegram upload throughput from the very first tick alongside automated 1TamilMV monitoring, full media processing, and multi-host DDL uploads.

## ⚡ Core Highlights

- **Max-Speed `cbgram 0.3.1` + `cbcrypto` Rust Engine:** Runs with `CBGRAM_RUST_CORE=1`, `CBGRAM_UVLOOP=1`, `CBGRAM_MAX_CONCURRENT=512`, `CBGRAM_WORKERS=128`, `BOT_THREAD_POOL_SIZE=256`, and parallel multi-part transfers (`max_parallel=16–64`) for instant full-bandwidth uploads.
- **Smart Telegram Destination & Dump Routing:**
  - Primary uploads use `USER_SESSION_STRING` to upload directly to `LEECH_LOG_CHAT` / `LEECH_DUMP_CHATS` (supports up to 4 GiB files with Telegram Premium).
  - User DM delivery and additional dump fan-out always use `TgClient.bot` cleanly without duplicate copies.
- **1TamilMV Auto-Leech (`/tmv`) & Quality Picker (`-tmv`):**
  - `/tmv` (respecting `CMD_SUFFIX`) in DM lets every user manage custom/comma-separated domains, **Pause/Resume** incoming-only monitoring, **User Settings** vs **Independent Settings** (full `/uset`-parity isolated menu), **Upload Destination** (`Telegram`, `Mirror`, `Uphoster`), and unlimited keyword-based **Dump Rules** (`name | chat_id`, `.` for all files).
  - `<topic_url> -tmv` opens a paginated quality picker with **Magnet**, **Torrent** (qBittorrent/Aria2), and **Cyberloom** filter tabs, cross-tab multi-select, and full flag support (`-e`, `-z`, `-d`, `-doc`, `-med`, `-n`, `-m`, `-up`, `-t`, etc.).
- **Built-in `/gensession`:** Step-by-step `cbgram` session string generator in DM for admins (`API ID` → `API Hash` → `+countrycode` phone → `OTP` → `2FA`).
- **DDL / Uphoster Support:** Native integrations for **GoFile**, **BuzzHeavier**, **PixelDrain**, **DevUploads**, **VikingFile**, and custom HTTP APIs, plus `/go?t=` pop-up redirect protection.
- **Telegram Premium Emojis & Arrow-Flow UI:** Automatic BotFather/custom emoji pack syncing and clean `━━━━━━━➤────` arrow-flow progress bars.

## 🚀 Quick VPS Docker Deployment

Export your credentials in the shell (shell exports always take top priority over `config.py` and `.env`), extract directly into your project directory, and run `./deploy.vps`:

```bash
unzip -o LMB-main-cbgram.zip && cp -rf LMB-main/* . && rm -rf LMB-main

export BOT_TOKEN="123456:ABC..."
export OWNER_ID="123456789"
export TELEGRAM_API="1234567"
export TELEGRAM_HASH="your_api_hash"
export USER_SESSION_STRING="your_cbgram_session"   # optional, for >2 GiB & fast dump uploads
export DATABASE_URL="mongodb+srv://..."            # optional, for persistent settings

chmod +x deploy.vps one-click-deploy.sh deploy.sh
./deploy.vps
```

Each directory deploys as an isolated Docker Compose project (`COMPOSE_PROJECT_NAME` = folder name) with persistent host mounts (`.sessions`, `downloads`, `thumbnails`, `tokens`, `rclone`, `cookies`, `Images`, `configs`, `logs`) and automatic free-port selection starting at `8080`.

### Useful Container Commands

```bash
sudo docker compose -p lmb ps
sudo docker compose -p lmb logs -f lmb
sudo docker compose -p lmb restart lmb
```

## ⚙️ Key Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CBGRAM_RUST_CORE` | `1` | Enable compiled Rust `cbcrypto` transfer & crypto engine |
| `CBGRAM_UVLOOP` | `1` | Enable `uvloop` event loop on Linux/macOS |
| `CBGRAM_MAX_CONCURRENT` | `512` | Max concurrent MTProto transmissions per client |
| `CBGRAM_WORKERS` | `128` | Client worker pool size |
| `BOT_THREAD_POOL_SIZE` | `256` | `ThreadPoolExecutor` workers for media/sync tasks |
| `USE_PREMIUM_EMOJIS` | `True` | Automatically render synced BotFather/custom `<tg-emoji>` |
| `PREMIUM_EMOJI_PACK` | `""` | Optional custom emoji pack short name(s) or `t.me/addemoji/...` |
| `CMD_SUFFIX` | `""` | Command suffix (e.g. `1` → `/leech1`, `/tmv1`, `/gensession1`) |
