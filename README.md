# LMB — Telegram Leech & Mirror Bot

High-speed Telegram Leech/Mirror Bot powered by **cbgram** + **cbcrypto** (Rust MTProto core + `uvloop`), with sequential no-skip Telegram uploads, multi-destination dump routing, video tools, and full cloud/hoster support.

---

## Key Features

### Telegram Upload & Routing
- **Strict Sequential Uploads:** Tasks run one after another and files within each task upload one by one in natural (`natsort`) order—eliminating file skipping, shuffling, and race conditions.
- **Bot Settings Dump Priority:** When `LEECH_LOG_CHAT` is configured, files always upload to the bot dump first (using `USER_SESSION_STRING` when available for `>2 GiB` support and full MTProto speed), then `TgClient.bot` copies sequentially one by one to the user's PM and all additional user/command/TMV dump destinations.
- **No-Skip Guardrails:** Safe 1024-char HTML caption capping, collision-free 64-char filename truncation, transient network retries, and automatic `send_document` fallback on invalid media dimensions/formats.

### Supported Downloaders & Resolvers
- **Torrents & Usenet:** qBittorrent, Aria2, SABnzbd (`nzbmirror` / `nzbleech`), and NZBHydra2 search (`/nzbsearch`).
- **Debrid & Cloud:** AllDebrid (`-ad`), TorBox (`-tb`), Seedr (`/slink`), Mega.nz, Google Drive, and Rclone.
- **Media & Galleries:** `yt-dlp` (`/ytdl` / `/ytdlleech`), `gallery-dl` (`/gdl` / `/gdlleech`), tl;dv meeting downloader, and JDownloader (`/jdmirror` / `/jdleech`).
- **Uphosters:** GoFile, PixelDrain, BuzzHeavier (with Account/Folder ID support), DevUploads, and VikingFile.

### Processing & Customization
- **FFmpeg & Video Tools:** Merge, trim, watermark, audio/subtitle track manager, sample video, screenshots, and custom `-ff` presets.
- **Filtering & Naming:** `INCLUDED_EXTENSIONS`, `EXCLUDED_EXTENSIONS`, `NAME_SWAP`, `AUTO_RENAME_FORMAT`, `SPLIT_EXT_SUFFIX` (`-ses`), `FILES_LINKS`, and custom captions/fonts/thumbnails.
- **Admin & Automation:** `/bsetting`, `/usetting`, `/gensession`, `/tmv`, `/leechdel`, `/broadcast`, `/rss`, and full `CMD_SUFFIX` support (with live `/` menu refresh).

---

## Commands Overview

| Category | Commands |
| :--- | :--- |
| **Mirror / Leech** | `/mirror` (`/m`), `/leech` (`/l`), `/qbmirror` (`/qm`), `/qbleech` (`/ql`), `/jdmirror` (`/jm`), `/jdleech` (`/jl`), `/nzbmirror` (`/nm`), `/nzbleech` (`/nl`) |
| **Media & Galleries** | `/ytdl` (`/y`), `/ytdlleech` (`/yl`), `/gallerydl` (`/gdl`), `/gallerydlleech` (`/gdlleech`), `/slink` |
| **Cloud & Management** | `/clone`, `/count`, `/del`, `/list`, `/search`, `/nzbsearch`, `/leechdel`, `/gdclean` |
| **Task & Status** | `/status` (`/s`), `/tstatus` (`/ts`), `/cancel` (`/c`), `/cancelall`, `/forcestart` (`/fs`), `/sel` |
| **Settings & Tools** | `/usetting` (`/us`), `/bsetting` (`/bs`), `/gensession` (`/gs`), `/tmv`, `/rss`, `/stats`, `/ping`, `/help` |

*All commands (except exempt public commands like `/start`, `/statusall`, `/cancelall`, `/restart`) automatically append `CMD_SUFFIX` when configured.*

---

## Quick Deployment (Docker / VPS)

```bash
# 1. Clone or extract the project and enter the directory
cd LMB-main

# 2. Configure config.py or environment variables (BOT_TOKEN, TELEGRAM_API, TELEGRAM_HASH, etc.)
# 3. Deploy via Docker Compose or VPS script:
docker compose up -d --build
# OR on a fresh VPS:
bash deploy.vps
```

### Performance Environment Defaults (`start.sh`)
- `CBGRAM_RUST_CORE=1`
- `CBGRAM_UVLOOP=1`
- `CBGRAM_MAX_CONCURRENT=512`
- `CBGRAM_WORKERS=64`
- `BOT_THREAD_POOL_SIZE=256`
