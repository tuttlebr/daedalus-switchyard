# syntax=docker/dockerfile:1.7

ARG RUST_VERSION=1.96.1
FROM rust:${RUST_VERSION}-bookworm AS builder

ARG SWITCHYARD_REPOSITORY=https://github.com/NVIDIA-NeMo/Switchyard.git
ARG SWITCHYARD_COMMIT=336196f6fbfc97ddc71c1700f6092e564e9f23c2

WORKDIR /opt/switchyard
RUN git init . \
    && git remote add origin "${SWITCHYARD_REPOSITORY}" \
    && git fetch --depth 1 origin "${SWITCHYARD_COMMIT}" \
    && test "$(git rev-parse FETCH_HEAD)" = "${SWITCHYARD_COMMIT}" \
    && git checkout --detach FETCH_HEAD

COPY patches/target-model-aliases.patch patches/fireworks-chat-reasoning.patch patches/composite-capable-fallback.patch patches/decision-api-classifier.patch /tmp/
RUN git apply --check /tmp/target-model-aliases.patch \
    && git apply /tmp/target-model-aliases.patch \
    && git apply --check /tmp/fireworks-chat-reasoning.patch \
    && git apply /tmp/fireworks-chat-reasoning.patch \
    && git apply --check /tmp/composite-capable-fallback.patch \
    && git apply /tmp/composite-capable-fallback.patch \
    && git apply --check /tmp/decision-api-classifier.patch \
    && git apply /tmp/decision-api-classifier.patch \
    && cargo build --locked --release -p switchyard-server

FROM debian:bookworm-slim

ARG SWITCHYARD_COMMIT=336196f6fbfc97ddc71c1700f6092e564e9f23c2

LABEL org.opencontainers.image.title="NVIDIA-NeMo Switchyard server" \
      org.opencontainers.image.description="Helm-deployable Switchyard v0.3.0 routing proxy" \
      org.opencontainers.image.source="https://github.com/NVIDIA-NeMo/Switchyard" \
      org.opencontainers.image.version="0.3.0" \
      org.opencontainers.image.revision="${SWITCHYARD_COMMIT}" \
      me.daedalus.switchyard.patches="target-model-aliases-v2,fireworks-chat-reasoning-v1,composite-capable-fallback-v1,decision-api-classifier-v2" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/switchyard/target/release/switchyard-server /usr/local/bin/switchyard-server

ENV HOME=/tmp

USER 1000:1000
EXPOSE 4000
STOPSIGNAL SIGTERM

ENTRYPOINT ["switchyard-server"]
