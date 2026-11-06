# syntax=docker/dockerfile:1

# NuttX image: the BTSak and NimBLE simulator firmware. The babblesim and
# runtime-base stages come from base.Dockerfile; build this file with
# docker/docker-bake.hcl, which links them.

# NuttX: prepare tools and source workspaces for sim:bthcisock and sim:nimble.
# https://nuttx.apache.org/docs/latest/quickstart/install.html
FROM babblesim AS nuttx
# Build arguments do not carry over from base.Dockerfile.
ARG BUILD_UID=1000
ARG BUILD_GID=1000
USER root

# kconfig-frontends provides kconfig-tweak for the documented test configurations.
# The simulator uses the inherited host/multilib compiler rather than an ARM toolchain.
# lib32z1-dev supplies the 32-bit zlib dependency for sim:nimble's FAT support.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    autoconf automake bison flex gettext texinfo gperf libtool pkg-config \
    libncurses-dev xxd genromfs kconfig-frontends \
    libgmp-dev libmpc-dev libmpfr-dev libisl-dev binutils-dev libelf-dev \
    libexpat1-dev picocom u-boot-tools util-linux \
    cmake ninja-build wget unzip xz-utils lib32z1-dev \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" \
    /opt/nuttx /opt/nuttx-apps /opt/nuttx-nimble /opt/nuttx-nimble-apps
USER bsworld

# Install Kconfiglib in the active virtual environment so Python can import it.
RUN python -m pip install kconfiglib

# Validated NuttX and apps revisions for both simulator test configurations.
# Fetch exact commits to avoid downloading the entire upstream Git history.
ARG NUTTX_REVISION=47180bf5ea00a94b41cf2083b8fed123199e5ce9
ARG NUTTX_APPS_REVISION=95833033f9f5c045309c1c48d2af994c78791f6b
ENV NUTTX_DIR=/opt/nuttx \
    NUTTX_APPS_DIR=/opt/nuttx-apps \
    NUTTX_NIMBLE_DIR=/opt/nuttx-nimble \
    NUTTX_NIMBLE_APPS_DIR=/opt/nuttx-nimble-apps

# Separate layers preserve a completed checkout if a later repository download fails.
RUN git init "$NUTTX_DIR" \
    && git -C "$NUTTX_DIR" remote add origin https://github.com/apache/nuttx.git \
    && git -C "$NUTTX_DIR" fetch --progress --depth=1 origin "$NUTTX_REVISION" \
    && git -C "$NUTTX_DIR" checkout --detach FETCH_HEAD \
    && test "$(git -C "$NUTTX_DIR" rev-parse HEAD)" = "$NUTTX_REVISION"

RUN git init "$NUTTX_APPS_DIR" \
    && git -C "$NUTTX_APPS_DIR" remote add origin https://github.com/apache/nuttx-apps.git \
    && git -C "$NUTTX_APPS_DIR" fetch --progress --depth=1 origin "$NUTTX_APPS_REVISION" \
    && git -C "$NUTTX_APPS_DIR" checkout --detach FETCH_HEAD \
    && test "$(git -C "$NUTTX_APPS_DIR" rev-parse HEAD)" = "$NUTTX_APPS_REVISION"

# Make builds NuttX in the source tree. Separate trees keep the two `nuttx`
# executables and apps objects independent without copying compiled binaries.
# Local clones reuse the prepared revisions without another network download.
RUN git clone "$NUTTX_DIR" "$NUTTX_NIMBLE_DIR" \
    && git clone "$NUTTX_APPS_DIR" "$NUTTX_NIMBLE_APPS_DIR"

# Keep independent NuttX/apps trees for both in-tree simulator configurations.
ARG BUILD_JOBS=4
RUN cd "$NUTTX_DIR" \
    && ./tools/configure.sh -l -a ../nuttx-apps sim:bthcisock \
    && kconfig-tweak --file .config -e SIM_BSIM_TIME -d BLUETOOTH_SMP_SELFTEST \
    && make olddefconfig \
    && make -j"$BUILD_JOBS" \
    && strip --strip-unneeded nuttx

# sim:nimble enables 32-bit compilation and FAT, which links against host zlib.
RUN cd "$NUTTX_NIMBLE_DIR" \
    && ./tools/configure.sh -l -a ../nuttx-nimble-apps sim:nimble \
    && kconfig-tweak --file .config -e SIM_BSIM_TIME \
        -d NIMBLE_PORTING_EXAMPLE -d EXAMPLES_NIMBLE \
        -e EXAMPLES_NIMBLE_BLEPRPH -e EXAMPLES_NIMBLE_BLECENT \
        -e NIMBLE_ROLE_PERIPHERAL -e NIMBLE_ROLE_CENTRAL -e NIMBLE_ROLE_OBSERVER \
    && make olddefconfig \
    && make -j"$BUILD_JOBS" \
    && strip --strip-unneeded nuttx

# The image runs its suites by default.
FROM runtime-base AS runtime-nuttx
COPY --from=nuttx /opt/nuttx/nuttx /opt/firmware/nuttx/btsak
COPY --from=nuttx /opt/nuttx-nimble/nuttx /opt/firmware/nuttx/nimble
ENV BSWORLD_NUTTX_BTSAK_BIN=/opt/firmware/nuttx/btsak \
    BSWORLD_NUTTX_NIMBLE_BIN=/opt/firmware/nuttx/nimble
RUN bsworld-check-binaries /opt/firmware/nuttx/btsak /opt/firmware/nuttx/nimble
CMD ["pytest", "tests/integration/nuttx", "-n", "auto", "--out-dir", "/output", "--junitxml=/output/pytest.xml"]
