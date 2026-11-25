FROM mysterysd/wzmlx:wzadv
COPY --from=mysterysd/wzmlx:m-tools /usr/local /usr/local
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /usr/src/app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN test -x /wzvenv/bin/python || (echo "ERROR: base image does not provide /wzvenv/bin/python" >&2; exit 1)

COPY requirements.txt .
RUN apt-get update -qq && apt-get install -y \
    build-essential gcc libc6-dev pkg-config \
    libmagic1 libmagic-dev libffi-dev libssl-dev \
    aria2 ffmpeg p7zip-full par2 rclone qbittorrent-nox sabnzbdplus \
    cpulimit mediainfo curl procps lsof iproute2 git wget && \
    (apt-cache show p7zip-rar >/dev/null 2>&1 && apt-get install -y p7zip-rar || true) && \
    rm -rf /var/lib/apt/lists/* && \
    rm -f /usr/local/bin/aria2c && \
    for bin in ffmpeg 7z rclone qbittorrent-nox; do \
      if [ -e "/usr/local/bin/$bin" ] && ! "/usr/local/bin/$bin" --version >/dev/null 2>&1 && [ -x "/usr/bin/$bin" ]; then \
        rm -f "/usr/local/bin/$bin"; \
      fi; \
    done && \
    aria2c --version >/dev/null && ffmpeg -version >/dev/null && 7z --help >/dev/null && rclone --version >/dev/null && qbittorrent-nox --version >/dev/null && \
    PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 uv pip uninstall --python /wzvenv/bin/python -y pyrogram kurigram pyrofork wzgram wzxpyro || true && \
    PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 uv pip install --python /wzvenv/bin/python --no-cache-dir -U -r requirements.txt && \
    PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 uv pip install --python /wzvenv/bin/python --no-cache-dir -U "cbgram[fast]" cbcrypto
ENV CANTARELLABOTS_MEGA_SDK_VERSION=10.20.20
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir --no-deps "mega.py>=1.0.8"

COPY . .

CMD ["bash", "start.sh"]
