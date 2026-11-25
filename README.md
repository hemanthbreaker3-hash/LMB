# 💎 LMB — High-Speed Telegram Mirror & Leech Bot

**LMB** is a production-ready Telegram **Mirror / Leech** bot for Linux VPS and Docker.  
It is powered by **[cbgram](https://cbgram.pages.dev/)** + **cbcrypto** (Rust MTProto crypto & transfer engine) for maximum upload/download speed under heavy multi-user load.

---

## ✨ Highlights

| Area | What you get |
|------|----------------|
| **Speed** | cbgram Rust core, Hyper multi-client transfers, full concurrency from the first part |
| **Scale** | Tuned for high concurrent users (`CBGRAM_MAX_CONCURRENT=1024`, workers `256`) + shared FloodWait guard |
| **1TamilMV** | `/tmv` auto-leech with Independent settings, **DL size limit**, **ZIP size limit**, dumps, domain auto-detect |
| **Status** | Normal tasks → `/status` · TMV auto-leech only → `/tstatus` or `/ts` |
| **Settings** | Full per-user `/uset` / `/us` menu (leech, mirror, FFmpeg, uphoster, watermark, …) |
| **DDL** | GoFile, BuzzHeavier, PixelDrain, DevUploads, VikingFile, custom HTTP APIs |

---

## 🚀 Quick Deploy (VPS + Docker)

```bash
unzip -o LMB-*.zip && cp -rf LMB-main/* . && rm -rf LMB-main

export BOT_TOKEN="123456:ABC..."
export OWNER_ID="123456789"
export TELEGRAM_API="1234567"
export TELEGRAM_HASH="your_api_hash"
export USER_SESSION_STRING="your_cbgram_session"   # optional; needed for >2 GiB & fast dumps
export DATABASE_URL="mongodb+srv://..."            # optional; persistent settings

# Performance (recommended)
export CBGRAM_RUST_CORE=1
export CBGRAM_UVLOOP=1
export CBGRAM_MAX_CONCURRENT=1024
export CBGRAM_WORKERS=256

chmod +x deploy.vps one-click-deploy.sh deploy.sh
./deploy.vps
```

```bash
sudo docker compose -p lmb ps
sudo docker compose -p lmb logs -f lmb
sudo docker compose -p lmb restart lmb
```

---

## ⚙️ Important environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CBGRAM_RUST_CORE` | `1` | Rust `cbcrypto` engine for AES + file transfer |
| `CBGRAM_UVLOOP` | `1` | uvloop event loop (Linux / macOS) |
| `CBGRAM_MAX_CONCURRENT` | `1024` | Concurrent MTProto transmissions per client |
| `CBGRAM_WORKERS` | `256` | Client worker pool size |
| `CMD_SUFFIX` | `""` | Suffix for all commands (`1` → `/leech1`, `/tmv1`, `/tstatus1`) |
| `USER_SESSION_STRING` | — | User session for premium-size uploads & primary hop |
| `HELPER_TOKENS` / `HELPER_STRINGS` | — | Extra bot/user clients for parallel Hyper transfers |
| `LEECH_LOG_CHAT` | — | Primary archive chat for fast user-session uploads |

Install stack:

```bash
pip install -U "cbgram[fast]" cbcrypto
# Do not mix with pyrogram / kurigram / pyrofork
```

See also: `SCALE-10K-NOTES.md`, `UPLOAD-SPEED-NOTES.md`.

---

## 🎬 1TamilMV Auto-Leech (`/tmv`)

Open **`/tmv`** in DM with the bot.

### Home controls
- Start / Stop / Pause / Resume monitor  
- Auto-detect or set domains  
- Check now · Dump rules · Settings  

### Settings mode
- **User Settings** — uses your normal `/uset` profile  
- **Independent** — isolated TMV profile (upload dest, prefix/suffix/caption, filters, interval)

### Size limits (Independent mode)
| Button | Meaning |
|--------|---------|
| **📏 DL Limit** | Max size for normal (non-archive) monitor releases. Default **4 GiB**. `-1` / `unlimited` = no limit |
| **📦 ZIP Limit** | Max size when title looks like zip/rar/7z/tar. Default **unlimited** |

Oversized releases are **skipped** (logged) and never queued.

Set values like: `4GB`, `2GiB`, `500MB`, or `-1`.

### Status separation
- TMV auto-leech tasks **do not** appear in `/status`  
- Use **`/tstatus`** or **`/ts`** for TMV-only task list  

---

## 👤 User settings (`/uset` · `/us`)

Main menu sections:

- General · Mirror · Leech · ENC/COM/Watermark · Video Tools  
- Uphoster · FF Media · Misc  
- Export / Import / Reset  

TMV Independent mode opens the same structure under an isolated scope so it never overwrites your personal `/uset` values.

---

## 📋 Common commands

| Command | Purpose |
|---------|---------|
| `/leech` `/l` | Leech link / file to Telegram |
| `/mirror` `/m` | Mirror to Drive / Rclone |
| `/status` `/s` | Active non-TMV tasks |
| `/tstatus` `/ts` | TMV auto-leech tasks only |
| `/tmv` | 1TamilMV monitor control panel |
| `/uset` `/us` | Personal settings |
| `/cancel` `/c` | Cancel a task |
| `/gensession` | Generate cbgram session string (admin) |

All of the above respect `CMD_SUFFIX`.

---

## 🛠 Recent fixes in this tree

- **RecursionError** in `_get_ud` (user settings) — fixed  
- **TMV Independent UI** — DL Limit + ZIP Limit buttons + enforcement  
- **Status split** — `/tstatus` / `/ts` for 1TamilMV tasks  
- **FloodWait guard** — shared backoff across clients  
- **Fair helper selection** — least-loaded helpers for even user speeds  
- **cbgram defaults** — higher concurrent transmissions & workers  

---

## 📄 License

See `LICENSE` in this repository.
