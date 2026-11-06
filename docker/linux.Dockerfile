# syntax=docker/dockerfile:1

# Linux image: the User Mode Linux kernel, the guest's seed library and NimBLE
# Linux, with the BlueZ and D-Bus the guest runs. The build-base and
# runtime-base stages come from base.Dockerfile; build this file with
# docker/docker-bake.hcl, which links them.

# User Mode Linux: the pinned mainline kernel, built as a host program with the
# suite's configuration fragment, and the library that fixes the guest's seed.
FROM build-base AS uml
# Build arguments do not carry over from base.Dockerfile.
ARG BUILD_UID=1000
ARG BUILD_GID=1000
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bc bison flex \
    && rm -rf /var/lib/apt/lists/*
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/uml
USER bsworld

# The kernel revision validated by tests/integration/uml/README.md.
ARG LINUX_REVISION=f66469d9905b39d2c260a15d7d6db450f4db67c1
ENV LINUX_DIR=/opt/uml/linux
RUN git init "$LINUX_DIR" \
    && git -C "$LINUX_DIR" remote add origin https://github.com/torvalds/linux.git \
    && git -C "$LINUX_DIR" fetch --progress --depth=1 origin "$LINUX_REVISION" \
    && git -C "$LINUX_DIR" checkout --detach FETCH_HEAD \
    && test "$(git -C "$LINUX_DIR" rev-parse HEAD)" = "$LINUX_REVISION"

# Copy each input alone, so editing init.sh does not rebuild the kernel.
ARG BUILD_JOBS=4
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/uml/source/linux.fragment /opt/bsworld-build/uml/linux.fragment
RUN cd "$LINUX_DIR" \
    && make ARCH=um defconfig \
    && scripts/kconfig/merge_config.sh -m .config /opt/bsworld-build/uml/linux.fragment \
    && make ARCH=um olddefconfig \
    && make ARCH=um -j"$BUILD_JOBS" \
    && strip --strip-unneeded -o /opt/uml/kernel linux
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/uml/source/getrandom/getrandom.c /opt/bsworld-build/uml/getrandom.c
RUN cc -shared -fPIC -O2 -o /opt/uml/getrandom.so /opt/bsworld-build/uml/getrandom.c

# NimBLE Linux: the upstream Host example the UML guest runs. Upstream builds it
# 32-bit with -m32, which the 64-bit guest cannot run, so the flags are cleared.
# Its Makefile also compiles through ccache; CC and CXX call gcc directly.
FROM build-base AS nimble-linux
# Build arguments do not carry over from base.Dockerfile.
ARG BUILD_UID=1000
ARG BUILD_GID=1000
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libmbedtls-dev \
    && rm -rf /var/lib/apt/lists/*
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/nimble
USER bsworld

# The revision validated by tests/integration/uml/nimble/README.md.
ARG NIMBLE_REVISION=1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845
ENV NIMBLE_DIR=/opt/nimble
RUN git init "$NIMBLE_DIR" \
    && git -C "$NIMBLE_DIR" remote add origin https://github.com/apache/mynewt-nimble.git \
    && git -C "$NIMBLE_DIR" fetch --progress --depth=1 origin "$NIMBLE_REVISION" \
    && git -C "$NIMBLE_DIR" checkout --detach FETCH_HEAD \
    && test "$(git -C "$NIMBLE_DIR" rev-parse HEAD)" = "$NIMBLE_REVISION"

ARG BUILD_JOBS=4
RUN make -C "$NIMBLE_DIR/porting/examples/linux" CC=gcc CXX=g++ \
        NIMBLE_CFLAGS= NIMBLE_LDFLAGS= -j"$BUILD_JOBS" \
    && strip --strip-unneeded "$NIMBLE_DIR/porting/examples/linux/nimble-linux"

# The UML guest mounts this image's root read-only and runs its BlueZ, D-Bus and
# NimBLE. Docker mounts /dev/shm noexec, where UML would map its memory, so
# TMPDIR moves that file to /tmp.
FROM runtime-base AS runtime-linux
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bluez dbus psmisc \
    libmbedcrypto7t64 libmbedtls14t64 libmbedx509-1t64 \
    && rm -rf /var/lib/apt/lists/*
USER bsworld
COPY --from=uml /opt/uml/kernel /opt/firmware/uml/linux
COPY --from=uml /opt/uml/getrandom.so /opt/firmware/uml/getrandom.so
COPY --from=nimble-linux /opt/nimble/porting/examples/linux/nimble-linux /opt/firmware/uml/nimble-linux
ENV TMPDIR=/tmp \
    BSWORLD_UML_KERNEL=/opt/firmware/uml/linux \
    BSWORLD_UML_GETRANDOM=/opt/firmware/uml/getrandom.so \
    BSWORLD_UML_NIMBLE_BIN=/opt/firmware/uml/nimble-linux
RUN bsworld-check-binaries /opt/firmware/uml/linux /opt/firmware/uml/getrandom.so \
        /opt/firmware/uml/nimble-linux /usr/libexec/bluetooth/bluetoothd \
        /usr/bin/btattach /usr/bin/bluetoothctl /usr/bin/dbus-daemon /usr/bin/killall
CMD ["pytest", "tests/integration/uml", "-n", "auto", "--out-dir", "/output", "--junitxml=/output/pytest.xml"]
