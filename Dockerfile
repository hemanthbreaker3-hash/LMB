FROM mysterysd/wzmlx:wzadv
COPY --from=mysterysd/wzmlx:m-tools /usr/local /usr/local
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /usr/src/app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN test -x /wzvenv/bin/python || (echo "ERROR: base image does not provide /wzvenv/bin/python" >&2; exit 1)

COPY requirements.txt .
RUN apt-get update -qq && apt-get install -y --no-install-recommends build-essential gcc pkg-config && rm -rf /var/lib/apt/lists/* && \
    PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 uv pip uninstall --python /wzvenv/bin/python -y pyrogram kurigram pyrofork wzgram wzxpyro || true && \
    PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 uv pip install --python /wzvenv/bin/python --no-cache-dir -U -r requirements.txt && \
    PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 uv pip install --python /wzvenv/bin/python --no-cache-dir -U "cbgram[fast]" cbcrypto
ENV CANTARELLABOTS_MEGA_SDK_VERSION=10.20.20
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir --no-deps "mega.py>=1.0.8"

COPY . .

CMD ["bash", "start.sh"]
