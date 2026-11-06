# syntax=docker/dockerfile:1

# TrouBLE image: the native Rust Host executable. The babblesim and
# runtime-base stages come from base.Dockerfile; build this file with
# docker/docker-bake.hcl, which links them.

# Build the native adapter against its pinned upstream Host commit. The adapter
# joins the BabbleSim runtime directly.
FROM babblesim AS trouble
# Build arguments do not carry over from base.Dockerfile.
ARG BUILD_UID=1000
ARG BUILD_GID=1000
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl ca-certificates gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/trouble /opt/rust
USER bsworld

# rust-toolchain.toml in the adapter directory selects the channel; rustup only
# needs to exist. Keep both homes writable by the build user.
ENV RUSTUP_HOME=/opt/rust/rustup \
    CARGO_HOME=/opt/rust/cargo \
    PATH=/opt/rust/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --default-toolchain none

COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/trouble/source /opt/trouble
# Share the registry and git checkouts across rebuilds; the pinned rev and the
# committed lock file make the resolved graph identical regardless of cache state.
RUN --mount=type=cache,id=bsworld-cargo-${BUILD_UID}-${BUILD_GID},target=/opt/rust/cargo/registry,uid=${BUILD_UID},gid=${BUILD_GID},sharing=locked \
    cd /opt/trouble \
    && cargo build --locked --release \
    && test -x /opt/trouble/target/release/ble_bas

# The image runs its suites by default.
FROM runtime-base AS runtime-trouble
COPY --from=trouble /opt/trouble/target/release/ble_bas /opt/firmware/trouble/ble_bas
ENV BSWORLD_TROUBLE_NATIVE_BIN=/opt/firmware/trouble/ble_bas
RUN bsworld-check-binaries /opt/firmware/trouble/ble_bas
CMD ["pytest", "tests/integration/trouble", "-n", "auto", "--out-dir", "/output", "--junitxml=/output/pytest.xml"]
