# syntax=docker/dockerfile:1.7

# ---------- Python dependencies ----------
FROM python:3.11-slim-bookworm AS py-builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip wheel --wheel-dir /wheels -r requirements.txt

# ---------- Bot runtime ----------
FROM python:3.11-slim-bookworm AS bot
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=py-builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels
COPY --chown=1000:1000 config.py logging_setup.py main.py webhook_server.py ./
COPY --chown=1000:1000 db ./db
COPY --chown=1000:1000 services ./services
COPY --chown=1000:1000 middlewares ./middlewares
COPY --chown=1000:1000 handlers ./handlers
COPY --chown=1000:1000 api ./api
COPY --chown=1000:1000 keyboards ./keyboards
COPY --chown=1000:1000 legal ./legal
COPY --chown=1000:1000 scripts/db_tool.py ./scripts/db_tool.py
COPY --chown=1000:1000 scripts/db_maintenance.py ./scripts/db_maintenance.py
COPY --chown=1000:1000 scripts/send_backup.py ./scripts/send_backup.py
COPY --chown=1000:1000 branding/telegram/welcome-1280x720.jpg ./branding/telegram/welcome-1280x720.jpg
RUN mkdir -p /app/data /app/logs && chown 1000:1000 /app/data /app/logs
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "main.py"]

# ---------- Frontends ----------
FROM node:22-bookworm-slim AS pwa-builder
WORKDIR /pwa
COPY pwa/package.json pwa/package-lock.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --no-audit --no-fund
COPY pwa/ ./
ARG VITE_BOT_USERNAME
ENV VITE_BOT_USERNAME=${VITE_BOT_USERNAME}
# The site's address for link previews (og:image must be absolute).
ARG PUBLIC_ORIGIN=https://app.cyberdefency.com
RUN npm run build

FROM node:22-bookworm-slim AS tma-builder
WORKDIR /tma
COPY tma/package.json tma/package-lock.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --no-audit --no-fund
COPY tma/ ./
ARG VITE_BOT_USERNAME
ENV VITE_BOT_USERNAME=${VITE_BOT_USERNAME}
RUN npm run build

# ---------- Legal documents as readable pages (stdlib only) ----------
FROM python:3.11-slim-bookworm AS legal-builder
WORKDIR /build
COPY scripts/render_legal.py ./
COPY scripts/legal_page ./legal_page
COPY legal/oferta.md legal/privacy.md ./legal/
ARG PUBLIC_ORIGIN=https://app.cyberdefency.com
RUN python render_legal.py --source legal --out /out --origin "$PUBLIC_ORIGIN"

# ---------- Caddy with rate limiting ----------
FROM caddy:2.11.7-builder AS caddy-builder
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    xcaddy build v2.11.7 --with github.com/mholt/caddy-ratelimit@v0.1.0

FROM caddy:2.11.7 AS web
COPY --from=caddy-builder /usr/bin/caddy /usr/bin/caddy
# Rootless loopback deployment uses an unprivileged port and drops all caps.
# The regular public Compose service grants NET_BIND_SERVICE explicitly.
RUN setcap -r /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=pwa-builder /pwa/dist /srv/pwa
COPY --from=tma-builder /tma/dist /srv/tma
COPY legal/oferta.md /srv/legal/oferta.md
COPY legal/privacy.md /srv/legal/privacy.md
COPY --from=legal-builder /out/oferta.html /srv/legal/oferta.html
COPY --from=legal-builder /out/privacy.html /srv/legal/privacy.html
