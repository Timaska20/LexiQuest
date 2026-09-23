FROM node:22-bookworm-slim

ARG VOT_JS_VERSION=latest
ENV NODE_ENV=production \
    PORT=3100 \
    VOT_POLL_TIMEOUT_MS=180000 \
    VOT_POLL_INTERVAL_MS=5000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 tini \
    && curl -L --fail --retry 3 https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
       -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp \
    && rm -rf /var/lib/apt/lists/*

# Install the Node VOT package first and keep its own Undici dependency tree.
# The bridge intentionally uses that npm Undici rather than Node's bundled
# fetch implementation (mixing them caused UND_ERR_INVALID_ARG on Node 22).
# Core is pinned to the exact resolved VOT version for the worker-provider
# subpath import.
RUN npm init -y >/dev/null 2>&1 \
    && npm pkg set type=module >/dev/null \
    && npm install --omit=dev "@vot.js/node@${VOT_JS_VERSION}" \
    && VOT_RESOLVED="$(node -p "require('./node_modules/@vot.js/node/package.json').version")" \
    && npm install --omit=dev "@vot.js/core@${VOT_RESOLVED}" \
    && node -e "require.resolve('undici')" \
    && printf '%s\n' "$VOT_RESOLVED" > /app/vot-version.txt

COPY server.mjs /app/server.mjs

EXPOSE 3100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3100/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "/app/server.mjs"]
