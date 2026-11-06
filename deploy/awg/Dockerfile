# syntax=docker/dockerfile:1.7
# Subscriber AmneziaWG server: userspace amneziawg-go + awg tools + agent.
# Separate from any existing Amnezia container on the host.

ARG AWG_GO_VERSION=v3.1.20260828
ARG AWG_TOOLS_VERSION=v3.1.20260812

FROM golang:1.25-alpine AS go-build
ARG AWG_GO_VERSION
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    apk add --no-cache git make bash
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      export GIT_SSL_CAINFO=/run/secrets/proxy_ca SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    git clone --depth 1 --branch "${AWG_GO_VERSION}" \
      https://github.com/amnezia-vpn/amneziawg-go /src/amneziawg-go \
    && cd /src/amneziawg-go && CGO_ENABLED=0 make

FROM alpine:3.20 AS tools-build
ARG AWG_TOOLS_VERSION
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    apk add --no-cache git build-base linux-headers bash
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export GIT_SSL_CAINFO=/run/secrets/proxy_ca; fi; \
    git clone --depth 1 --branch "${AWG_TOOLS_VERSION}" \
      https://github.com/amnezia-vpn/amneziawg-tools /src/amneziawg-tools \
    && make -C /src/amneziawg-tools/src

FROM alpine:3.20
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    apk add --no-cache python3 iproute2 iptables
COPY --from=go-build /src/amneziawg-go/amneziawg-go /usr/bin/amneziawg-go
COPY --from=tools-build /src/amneziawg-tools/src/wg /usr/bin/awg
COPY agent.py /opt/awg/agent.py
# Userspace implementation on purpose: no kernel module needs to be installed.
ENV WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4).status == 200 else 1)"
CMD ["python3", "/opt/awg/agent.py"]
