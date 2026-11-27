# LMB — Telegram Leech & Mirror Bot

High-speed Telegram Leech/Mirror Bot powered by the latest **cbgram** + **cbcrypto** (Rust MTProto core + `uvloop`), featuring multi-lane ultra-fast Telegram downloads, strict sequential no-skip Telegram uploads, multi-destination dump routing, video tools, and full cloud/hoster support.

---

## Key Features

### Ultra-Fast Telegram Downloads (`HypertgDownload`)
- **Multi-Lane Parallel MTProto Sessions:** Opens independent parallel TCP media sessions (`lane=i`) across `TgClient.bot`, `TgClient.user`, and configured helper bots/users—saturating Telegram DC bandwidth even without extra helper tokens or dump chats.
- **Balanced 1 MiB Pipelining:** Uses Telegram's maximum `1 MiB` `GetFile` chunk size from byte 0 with a balanced `32–64` in-flight window per lane, avoiding DC socket stalls for sustained, stable high-speed downloads.

### Sequential No-Skip Telegram Uploads & Dump Routing
- **One-by-One Sequential Uploads:** Tasks execute one after another and files within each task upload one by one in `natsort` order—eliminating file skipping, shuffling, and race conditions.
- **Bot Settings Dump Priority:** When `LEECH_LOG_CHAT` is configured, files always upload to the bot dump first (using `USER_SESSION_STRING` when available for `>2 GiB` support), then `TgClient.bot` copies sequentially one by one to the user's PM and all additional user/command/TMV dump destinations.
- **No-Skip Guardrails:** Safe 1024-char HTML caption capping, collision-free 64-char filename truncation, transient network retries, and automatic `send_document` fallback on invalid media dimensions/formats.
- **Auto Thumbnail & Cover:** TMDb-powered `auto_thumb.jpg` (`<=320px`, `<200KB`) and full-resolution `auto_cover.jpg` (`video_cover`) in the user's Telegram language.

### Supported Downloaders, Resolvers & Tools
- **Torrents & Usenet:** qBittorrent, Aria2, SABnzbd (`/nzbmirror` / `/nzbleech`), and NZBHydra2 (`/nzbsearch`).
- **Debrid & Cloud:** AllDebrid (`-ad`), TorBox (`-tb`), Seedr (`/slink`), Mega.nz, Google Drive, and Rclone.
- **Media & Galleries:** `yt-dlp` (`/ytdl` / `/ytdlleech`), `gallery-dl` (`/gdl` / `/gdlleech`), tl;dv meeting downloader, and JDownloader (`/jdmirror` / `/jdleech`).
- **Uphosters:** GoFile, PixelDrain, BuzzHeavier (with Account/Folder ID support), DevUploads, and VikingFile.
- **Video Tools (`-vt`):** Video + Video, Video + Audio, Video + Subtitle (with Hardsub & Font Style), Trim, Watermark, SubSync, Audio Split, and Track Manager (`-tm`).
- **Batch & Task Controls:** `-i` (multi-task), `-b` (bulk), `-m` (merge folder), `-ses` (split ext suffix), `INCLUDED_EXTENSIONS`, `EXCLUDED_EXTENSIONS`, `FILES_LINKS`, instant no-confirmation Cancel buttons, `/leechdel`, `/gensession`, `/tmv`, `/restart`, and full `CMD_SUFFIX` support.

---

## Quick Deployment (Docker / VPS)

```bash
cd LMB-main
bash deploy.vps
# OR with Docker Compose directly:
docker compose up -d --build
```

### Performance Environment Defaults (`start.sh`)
- `CBGRAM_RUST_CORE=1`
- `CBGRAM_UVLOOP=1`
- `CBGRAM_MAX_CONCURRENT=512`
- `CBGRAM_WORKERS=64`
- `BOT_THREAD_POOL_SIZE=256`
