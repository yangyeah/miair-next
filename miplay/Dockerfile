ARG BASE_IMAGE=mrdeer1997/miair-next:latest
FROM ${BASE_IMAGE} AS builder
USER root
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
RUN curl --fail --show-error --location https://sh.rustup.rs -o /tmp/rustup.sh \
    && sh /tmp/rustup.sh -y --profile minimal --default-toolchain 1.100.0 --no-modify-path
ENV PATH=/opt/cargo/bin:${PATH}
WORKDIR /src
COPY miplay/vendor /src/vendor
COPY miplay/receiver /src/receiver
ENV CARGO_BUILD_JOBS=1 CARGO_TARGET_DIR=/src/target
RUN --mount=type=cache,target=/opt/cargo/registry --mount=type=cache,target=/src/target \
    cargo build --locked --release --manifest-path /src/receiver/Cargo.toml \
    && cargo test --locked --release --manifest-path /src/vendor/FusionPlay.MiPlaySdk/Cargo.toml --no-default-features --features headless \
    && cp /src/target/release/miair-miplay-receiver /usr/local/bin/miair-miplay-receiver

FROM ${BASE_IMAGE}
COPY --from=builder /usr/local/bin/miair-miplay-receiver /usr/local/bin/miair-miplay-receiver
COPY backend/app /opt/miplay/app
COPY miplay/bridge.py /opt/miplay/bridge.py
COPY miplay/LICENSE miplay/NOTICE /usr/share/licenses/miplay/
WORKDIR /opt/miplay
ENV PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.source="https://github.com/yangyeah/miair-next" \
      org.opencontainers.image.licenses="AGPL-3.0-only AND GPL-3.0-or-later"
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import json,urllib.request; c=json.load(open('/run/miplay/config.json')); urllib.request.urlopen('http://127.0.0.1:%s/health'%c.get('health_port',18301),timeout=2)"
ENTRYPOINT ["python", "/opt/miplay/bridge.py"]
