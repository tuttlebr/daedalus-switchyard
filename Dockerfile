# syntax=docker/dockerfile:1.7

ARG RUST_VERSION=1.96.1
FROM rust:${RUST_VERSION}-bookworm AS builder

ARG SWITCHYARD_REPOSITORY=https://github.com/NVIDIA-NeMo/Switchyard.git
ARG SWITCHYARD_COMMIT=1fc9ab887d1c663b0048ae24d5f473d15ed8daaa

WORKDIR /opt/switchyard
RUN git init . \
    && git remote add origin "${SWITCHYARD_REPOSITORY}" \
    && git fetch --depth 1 origin "${SWITCHYARD_COMMIT}" \
    && test "$(git rev-parse FETCH_HEAD)" = "${SWITCHYARD_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && cargo build --locked --release -p switchyard-server

COPY patches/target-model-aliases.patch /tmp/target-model-aliases.patch
RUN git apply --check /tmp/target-model-aliases.patch \
    && git apply /tmp/target-model-aliases.patch \
    && cargo build --locked --release -p switchyard-server

FROM debian:bookworm-slim

ARG SWITCHYARD_COMMIT=1fc9ab887d1c663b0048ae24d5f473d15ed8daaa

LABEL org.opencontainers.image.title="NVIDIA-NeMo Switchyard server" \
      org.opencontainers.image.description="Helm-deployable Switchyard v0.2.0 routing proxy" \
      org.opencontainers.image.source="https://github.com/NVIDIA-NeMo/Switchyard" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.revision="${SWITCHYARD_COMMIT}" \
      me.daedalus.switchyard.patches="target-model-aliases-v1" \
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
