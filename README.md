# LMB

**LMB** is a high-performance Telegram mirror and leech bot built for Linux VPS deployments. Powered by the **`wzgram`** MTProto framework (`WarpCrypto` + parallel media sessions), LMB delivers maximum download and instant Telegram upload throughput alongside full media processing, user settings, verification controls, and smart resource-aware task queueing.

> **Deployment Note:** Reliable operation requires valid Telegram credentials (`BOT_TOKEN`, `TELEGRAM_API`, `TELEGRAM_HASH`), a reachable MongoDB instance (`DATABASE_URL`), supported system packages (`ffmpeg`, `aria2c`, `qbittorrent-nox`, `rclone`, `7z`), and a properly configured Linux VPS.

## Key Features

- **High-Speed Transfers (`wzgram` MTProto):** Uses `wzgram[fast]` with `WarpCrypto` Rust cryptography, 512 KiB `SaveBigFilePart` chunks, 8 pre-warmed media sessions (up to 48 concurrent workers), `WZGRAM_MAX_CONCURRENT=16`, and zero artificial upload delays for immediate, full-speed Telegram uploads.
- **Maximized Download Engines:**
  - **Telegram (`HypertgDownload`):** 1 MiB `GetFile` chunks from the first request, deep pipelining, and multi-client helper pooling.
  - **Aria2 / Direct HTTP (`a2c.conf`):** 32 connections per server, 32 splits, 512 KiB minimum split size, 512 MiB disk cache, and `optimize-concurrent-downloads=true`.
  - **yt-dlp:** 24 concurrent fragment downloads, 16 MiB HTTP chunks, and 2 MiB socket buffers.
  - **qBittorrent, SABnzbd, JDownloader, Mega, Google Drive, Rclone, and Seedr.**
- **Smart Resource-Aware Queue (`Queue DL`):** Tasks run immediately without artificial single-user limits (`USER_MAX_TASKS=0`, `BOT_MAX_TASKS=0` by default). Automatic download queueing only engages when the host is under critical memory pressure (RAM usage `>= 88%` or available RAM `< 160 MiB` while tasks are active) or when explicit queue limits are configured.
- **Video Processing (`-vt`):** Video merge, audio split, subtitle sync, compression, conversion, watermarking, multi-range trimming, extraction, ad/name removal, and binary media splitting.
- **Audio & Subtitle Track Manager (`-tm`):** Remux, reorder, remove, or edit audio/subtitle track metadata without re-encoding.
- **Automatic Media Thumbnails:** Extracts clean titles from release filenames and fetches verified backdrop/poster artwork from IMDb, TMDb, AniList, Jikan, and TVmaze with a strict 3-second timeout so artwork lookups never delay uploads.
- **Per-User Settings & Verification:** Persistent MongoDB configuration, verification shorteners, `CMD_SUFFIX` support, and clean Telegram UI.

## Audio Split

Audio Split is opt-in. When disabled, incoming tasks preserve all original audio streams.

Enable **Audio Split on Incoming Tasks** under **User Settings → Video Processing Tools** to apply it automatically after downloads finish across all download sources (`TaskListener.on_download_complete`):

- **Auto Mode:** Detects every audio stream and produces one output video per stream using stream copy (`-c copy`, zero re-encoding).
- **Manual Mode:** Reveals the **Set Languages** button. Enter comma-separated language codes or names (for example `TEL,TAM,HIN`). Each matching track gets its own output video, while non-matching tracks are preserved together in an additional output video. Send `clear` to remove the saved list.
- If none of the configured languages match, or if usable streams cannot be isolated, the original file is kept unchanged.

## Video Tools (`-vt`)

Supported interactive `-vt` workflows include:

- Video + Video Merge
- Audio Split
- SubSync & Audio Sync
- Compress & Convert
- Watermark
- Extract & **Multi-Range Trim**
- Remove Ads & Names
- Media Split → Binary
- Multi-VT where supported

Multi-range trim accepts one or more timestamp ranges:

```text
00:01:00 00:02:30
00:01:00-00:02:30, 00:05:00-00:06:15, 01:10:00-01:12:00
```

Each range produces its own clip when multiple ranges are supplied.

## Audio & Subtitle Track Manager (`-tm`)

```text
/mirror https://example.com/video.mkv -tm
```

Select or remove audio/subtitle streams, reorder tracks, edit language/title metadata, set default or forced flags, and apply compatible layouts across multiple files without re-encoding.

## Verification

```text
/verify
/verify on 7d
/verify off
/verify time 7d
/verify time never
/verify status
/verify user USER_ID 7d
/verify user USER_ID revoke
```

Authorized administrators can manage verification shorteners via `/shorteners`.

## Configuration

Configure required settings in `config.py` or via environment variables:

```bash
export BOT_TOKEN='YOUR_TELEGRAM_BOT_TOKEN'
export TELEGRAM_API='YOUR_TELEGRAM_API_ID'
export TELEGRAM_HASH='YOUR_TELEGRAM_API_HASH'
export OWNER_ID='YOUR_TELEGRAM_USER_ID'
export DATABASE_URL='YOUR_MONGODB_URI'
export CMD_SUFFIX='a'
```

Optional start-menu customization variables: `START_REPO_URL`, `START_REPO_BUTTON`, `START_UPDATE_URL`, `START_UPDATE_BUTTON`, `START_DEV_URL`, and `START_DEV_BUTTON`.

Optional speed tuning variables:
- `USER_SESSION_STRING` — Telegram Premium user session for >2 GiB uploads and high-speed primary hops.
- `HELPER_TOKENS` / `HELPER_STRINGS` — Extra bot/user clients for parallel `HypertgDownload` and `HypertgUpload`.
- `WZGRAM_MAX_CONCURRENT` — Concurrent transmissions per client (default `16`).
- `WZGRAM_FAST_UPLOAD_SESSIONS` — Parallel media sessions per upload (default `8`, ceiling `16`).

Never commit real bot tokens, API hashes, session strings, or database URIs to version control.

## Deploy on Ubuntu / Debian VPS

### 1. Prepare the source

Extract the project archive (or clone your repository) and enter the project directory:

```bash
cd LMB-main
```

### 2. Configure credentials

Set the environment variables above or update `config.py`. Ensure your MongoDB URI is reachable from the VPS.

### 3. Install & start

```bash
chmod +x one-click-deploy.sh deploy.vps deploy-user.vps start.sh
./one-click-deploy.sh
```

Run as root for system package installation and `systemd` setup.

## Operations

### systemd

```bash
systemctl status cantarellabots_bot --no-pager -l
journalctl -u cantarellabots_bot -f
```

### User-local process

```bash
./run-user.sh
tail -f logs/user-bot.log
kill "$(cat .cdmlx-user.pid)"
```

## Troubleshooting

- **Bot does not start:** Verify `config.py`, environment variables, and the first Python traceback in the service logs.
- **Syntax / import check:** Run `python3 -m compileall -q bot` from the repository root.
- **Database connection error:** Check `DATABASE_URL`, MongoDB Atlas Network Access rules, and database user credentials.
- **FFmpeg error:** Confirm `ffmpeg` and `ffprobe` are installed and available in `PATH`.
- **Audio Split misses a language:** Inspect stream tags with `ffprobe` and ensure audio streams carry standard language metadata (e.g., `tel`, `tam`, `hin`, `eng`, `kan`, `mal`).

## Developer Checks

```bash
python3 -m compileall -q bot
```

Verify download sources (Aria2, qBittorrent, NZB, JDownloader, Mega, Google Drive, Rclone, yt-dlp, Telegram), mirror/leech workflows, `-vt`, Audio Split, and `-tm` after updates.
