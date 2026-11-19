# AMD64 test runner and 32-bit UML guests.

# Keep project dependencies out of the common firmware build environment.
FROM public.ecr.aws/docker/library/ubuntu:24.04 AS build-base

# Python headers and a native compiler support packages with C/C++ extensions.
# DEBIAN_FRONTEND applies only to installation, not to interactive containers.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash ca-certificates curl git file build-essential \
    python3.12 python3.12-dev python3.12-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Match the invoking user's UID/GID for writable bind mounts when building locally.
# Reuse Ubuntu's existing account if it already owns the requested UID.
ARG BUILD_UID=1000
ARG BUILD_GID=1000
RUN if ! getent group "$BUILD_GID" >/dev/null; then groupadd -g "$BUILD_GID" bsworld; fi \
    && if getent passwd "$BUILD_UID" >/dev/null; then \
        build_user="$(getent passwd "$BUILD_UID" | cut -d: -f1)"; \
        usermod -l bsworld -d /home/bsworld -m -g "$BUILD_GID" "$build_user"; \
    else useradd -m -u "$BUILD_UID" -g "$BUILD_GID" -s /bin/bash bsworld; fi \
    && mkdir -p /opt/venv /workspace /output /cache/pip /cache/downloads \
    && chown -R "$BUILD_UID:$BUILD_GID" /opt/venv /workspace /output /cache
USER bsworld

RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PIP_CACHE_DIR=/cache/pip

WORKDIR /workspace
CMD ["bash"]

# Multilib compiler for the UML kernel and Guest applications.
FROM build-base AS native-build
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gcc-multilib g++-multilib \
    && rm -rf /var/lib/apt/lists/*
USER bsworld

FROM native-build AS babblesim
USER root

# BabbleSim source checkout and shared PHY build tools.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    repo binutils make \
    coreutils findutils grep sed \
    && rm -rf /var/lib/apt/lists/*

USER bsworld

# Local and Docker builds use the same repo snapshot and upstream folder layout.
ENV BSIM_OUT_PATH=/opt/babblesim/build \
    BSIM_COMPONENTS_PATH=/opt/babblesim/components \
    BSIM_BUILD_FAIL_ASAP=1
USER root
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/babblesim
USER bsworld
COPY --chown=$BUILD_UID:$BUILD_GID components/babblesim.xml /opt/bsworld-build/babblesim.xml
RUN cd /opt/babblesim \
    && repo init --standalone-manifest --depth=1 -u file:///opt/bsworld-build/babblesim.xml \
    && repo sync -c --no-tags
# The bin directory also contains .Tsymbols text maps; strip only executables.
RUN make -C /opt/babblesim everything \
    && find "$BSIM_OUT_PATH/bin" -maxdepth 1 -type f -executable \
        -exec strip --strip-unneeded {} + \
    && strip --strip-unneeded "$BSIM_OUT_PATH"/lib/*.so

# Runtime Python tools and BabbleSim. Guest binaries and libraries live in
# their own root filesystem.
FROM build-base AS runtime-base
ENV PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1 \
    BSWORLD_ROOT=/workspace
COPY --chown=$BUILD_UID:$BUILD_GID . /workspace/
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/bsworld/
# Git metadata is excluded from images; CI supplies the Git-derived version.
ARG BSWORLD_VERSION=0+local
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSWORLD="$BSWORLD_VERSION" \
    python -m pip install -e components/bscontroller -e '.[test]'
USER root
# These are the runtime counterparts of the firmware stages' development packages.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    procps \
    libc6-i386 libstdc++6 zlib1g \
    && rm -rf /var/lib/apt/lists/*
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/babblesim/build/results

# Verify binaries and their dynamic dependencies in the final filesystem.
# BabbleSim .Tsymbols files are readable text maps, not executable binaries.
COPY --chmod=755 <<'EOF' /usr/local/bin/bsworld-check-binaries
#!/bin/sh
for binary in "$@"; do
    case "$binary" in *.Tsymbols) continue ;; esac
    if ! test -f "$binary" || ! test -x "$binary"; then
        printf 'Missing or non-executable binary: %s\n' "$binary"
        exit 1
    fi
    dependencies="$(ldd "$binary" 2>&1)"
    status=$?
    case "$dependencies" in
        *"not a dynamic executable"*|*"statically linked"*) ;;
        *) if test "$status" -ne 0; then
               printf 'ldd failed: %s\n%s\n' "$binary" "$dependencies"
               exit 1
           fi ;;
    esac
    if printf '%s\n' "$dependencies" | grep -q 'not found'; then
        printf 'Missing runtime dependencies: %s\n%s\n' "$binary" "$dependencies"
        exit 1
    fi
done
EOF

# Keep BabbleSim's source, scripts, and outputs together at their build-time paths.
COPY --from=babblesim --chown=$BUILD_UID:$BUILD_GID /opt/babblesim /opt/babblesim
ENV BSIM_OUT_PATH=/opt/babblesim/build \
    BSIM_COMPONENTS_PATH=/opt/babblesim/components
USER bsworld
RUN bsworld-check-binaries /opt/babblesim/build/bin/*

# Zephyr: follow the official Getting Started Guide for Ubuntu 24.04.
# https://docs.zephyrproject.org/latest/develop/getting_started/index.html
FROM native-build AS zephyr-uml
USER root

# git, file, make, GCC/multilib, Python headers, and venv support are inherited above.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cmake ninja-build gperf dfu-util device-tree-compiler \
    libmagic1 libsdl2-dev python3-tk \
    wget unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/zephyrproject
USER bsworld

# Match the typed-address and periodic-sync command interfaces used by the shell.
ARG ZEPHYR_REVISION=f545c1ca45bd0e5cb767d01996f5806f9f1e50be

# The workspace lives outside /workspace so mounting the BSWorld checkout keeps it available.
ENV ZEPHYR_WORKSPACE=/opt/zephyrproject \
    ZEPHYR_BASE=/opt/zephyrproject/zephyr

# Keep Zephyr's requirements separate from BSWorld's Python environment.
# Activate <workspace>/.venv when invoking west, then /opt/venv for BSWorld tests.
RUN python3.12 -m venv "$ZEPHYR_WORKSPACE/.venv" \
    && . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && python -m pip install west

# A commit SHA is not a branch name for git clone --branch. Fetch the exact
# commit first, then initialize west around the local manifest repository.
# Force download progress when Docker captures stderr without a terminal.
RUN git init "$ZEPHYR_BASE" \
    && git -C "$ZEPHYR_BASE" remote add origin https://github.com/zephyrproject-rtos/zephyr \
    && git -C "$ZEPHYR_BASE" fetch --progress --depth=1 origin "$ZEPHYR_REVISION" \
    && git -C "$ZEPHYR_BASE" checkout --detach FETCH_HEAD \
    && . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && west init -l "$ZEPHYR_BASE" \
    && test "$(git -C "$ZEPHYR_BASE" rev-parse HEAD)" = "$ZEPHYR_REVISION"

# Keep module downloads and Python package setup in a separate build layer.
# Fetch only the modules the native_sim Bluetooth shell uses.
ARG ZEPHYR_PROJECT_FILTER=-.*,+cmsis,+cmsis_6,+hal_nordic,+mbedtls,+tf-psa-crypto,+tf-psa-crypto-drivers,+picolibc
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west config manifest.project-filter -- "$ZEPHYR_PROJECT_FILTER" \
    && west update --narrow --fetch-opt=--depth=1 \
    && west packages pip --install \
    && west zephyr-export

# Build the 32-bit shell for the UML guest with the host compiler.
ARG BUILD_JOBS=4
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/uml/zephyr/prj.conf /opt/bsworld-build/zephyr/prj.conf
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && export CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS" ZEPHYR_TOOLCHAIN_VARIANT=host \
    && cd "$ZEPHYR_WORKSPACE" \
    && west build -b native_sim/native -d build/bsworld_uml_zephyr \
        "$ZEPHYR_BASE/tests/bluetooth/shell" \
        -- -DCONF_FILE=/opt/bsworld-build/zephyr/prj.conf \
    && strip --strip-unneeded build/bsworld_uml_zephyr/zephyr/zephyr.exe

# NuttX: prepare tools and source workspaces for sim:bthcisock and sim:nimble.
# https://nuttx.apache.org/docs/latest/quickstart/install.html
FROM native-build AS nuttx-uml
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

ARG BUILD_JOBS=4
RUN cd "$NUTTX_DIR" \
    && ./tools/configure.sh -l -a ../nuttx-apps sim:bthcisock \
    && kconfig-tweak --file .config -e SIM_M32 \
        -d BLUETOOTH_SMP_SELFTEST \
    && make olddefconfig \
    && make -j"$BUILD_JOBS" \
    && strip --strip-unneeded nuttx
RUN cd "$NUTTX_NIMBLE_DIR" \
    && ./tools/configure.sh -l -a ../nuttx-nimble-apps sim:nimble \
    && kconfig-tweak --file .config -e SIM_M32 \
        -d NIMBLE_PORTING_EXAMPLE -d EXAMPLES_NIMBLE \
        -e EXAMPLES_NIMBLE_BLEPRPH -e EXAMPLES_NIMBLE_BLECENT \
        -e NIMBLE_ROLE_PERIPHERAL -e NIMBLE_ROLE_CENTRAL -e NIMBLE_ROLE_OBSERVER \
    && make olddefconfig \
    && make -j"$BUILD_JOBS" \
    && strip --strip-unneeded nuttx

# User Mode Linux: the pinned mainline kernel, built as a host program with the
# suite's configuration fragment, and the library that fixes the guest's seed.
FROM native-build AS uml
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
COPY --chown=$BUILD_UID:$BUILD_GID src/bsworld/uml/linux.fragment /opt/bsworld-build/uml/linux.fragment
RUN cd "$LINUX_DIR" \
    && make ARCH=um SUBARCH=i386 defconfig \
    && scripts/kconfig/merge_config.sh -m .config /opt/bsworld-build/uml/linux.fragment \
    && make ARCH=um SUBARCH=i386 olddefconfig \
    && make ARCH=um SUBARCH=i386 -j"$BUILD_JOBS" \
    && strip --strip-unneeded -o /opt/uml/kernel linux
COPY --chown=$BUILD_UID:$BUILD_GID src/bsworld/uml/getrandom.c /opt/bsworld-build/uml/getrandom.c
RUN cc -m32 -shared -fPIC -O2 -o /opt/uml/getrandom.so /opt/bsworld-build/uml/getrandom.c

# NimBLE Linux: the upstream 32-bit Host demo. Use the compiler directly in
# place of the Makefile's ccache wrapper.
FROM native-build AS nimble-linux
USER root
RUN dpkg --add-architecture i386 \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libmbedtls-dev:i386 \
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
RUN make -C "$NIMBLE_DIR/porting/examples/linux" CC=gcc CXX=g++ -j"$BUILD_JOBS" \
    && strip --strip-unneeded "$NIMBLE_DIR/porting/examples/linux/nimble-linux"

# Build the upstream TrouBLE Linux examples for the UML guest.
FROM native-build AS trouble-uml
USER root
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/trouble /opt/rust
USER bsworld

ARG TROUBLE_REVISION=da44b34f67e1ee8efe977a5a975804b8b21962d4
ARG TROUBLE_RUST_VERSION=1.97.1
ENV RUSTUP_HOME=/opt/rust/rustup \
    CARGO_HOME=/opt/rust/cargo \
    PATH=/opt/rust/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --no-modify-path --default-toolchain none \
    && rm /tmp/rustup-init.sh \
    && rustup toolchain install "$TROUBLE_RUST_VERSION" --profile minimal \
        --target i686-unknown-linux-gnu

RUN git init /opt/trouble \
    && git -C /opt/trouble fetch --depth=1 https://github.com/embassy-rs/trouble.git "$TROUBLE_REVISION" \
    && git -C /opt/trouble checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/trouble rev-parse HEAD)" = "$TROUBLE_REVISION"

ARG BUILD_JOBS=4
# Duplicate Tokio waker vtables in this build cause repeated self-wakes in
# Embassy's waker registration. A single codegen unit keeps the vtable shared.
RUN --mount=type=cache,id=bsworld-trouble-cargo-${BUILD_UID}-${BUILD_GID},target=/opt/rust/cargo/registry,uid=${BUILD_UID},gid=${BUILD_GID},sharing=locked \
    cd /opt/trouble/examples/linux \
    && CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 cargo "+$TROUBLE_RUST_VERSION" build --locked --release -j"$BUILD_JOBS" \
        --target i686-unknown-linux-gnu --bin ble_bas_central --bin ble_bas_peripheral \
    && strip --strip-unneeded target/i686-unknown-linux-gnu/release/ble_bas_central \
        target/i686-unknown-linux-gnu/release/ble_bas_peripheral

# The guest's rootfs is assembled independently of the 64-bit test runner.
FROM build-base AS uml-rootfs
USER root
COPY docker/uml-rootfs.sh /opt/bsworld-build/uml/rootfs.sh
RUN apt-get -o APT::Architecture=i386 update \
    && sh /opt/bsworld-build/uml/rootfs.sh /opt/uml/rootfs \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 src/bsworld/uml/init.sh /opt/uml/rootfs/init
COPY tests/integration/uml/bluez/main.conf /opt/uml/rootfs/opt/bsworld/bluez.conf
COPY --from=nimble-linux /opt/nimble/porting/examples/linux/nimble-linux /opt/uml/rootfs/opt/bsworld/nimble-linux
COPY --from=zephyr-uml /opt/zephyrproject/build/bsworld_uml_zephyr/zephyr/zephyr.exe /opt/uml/rootfs/opt/bsworld/zephyr-shell
COPY --from=nuttx-uml /opt/nuttx/nuttx /opt/uml/rootfs/opt/bsworld/nuttx-btsak
COPY --from=nuttx-uml /opt/nuttx-nimble/nuttx /opt/uml/rootfs/opt/bsworld/nuttx-nimble
COPY --from=trouble-uml /opt/trouble/examples/linux/target/i686-unknown-linux-gnu/release/ble_bas_central \
    /opt/trouble/examples/linux/target/i686-unknown-linux-gnu/release/ble_bas_peripheral \
    /opt/uml/rootfs/opt/bsworld/trouble/

# TMPDIR avoids Docker's noexec mount on /dev/shm for UML's memory mapping.
FROM runtime-base AS runtime-linux
COPY --from=uml /opt/uml/kernel /opt/firmware/uml/linux
COPY --from=uml /opt/uml/getrandom.so /opt/firmware/uml/getrandom.so
COPY --from=uml-rootfs /opt/uml/rootfs /opt/firmware/uml/rootfs
COPY --from=trouble-uml /opt/trouble/LICENSE-APACHE /opt/trouble/LICENSE-MIT /usr/share/doc/bsworld/trouble/
ENV TMPDIR=/tmp \
    BSWORLD_UML_KERNEL=/opt/firmware/uml/linux \
    BSWORLD_UML_GETRANDOM=/opt/firmware/uml/getrandom.so \
    BSWORLD_UML_ROOTFS=/opt/firmware/uml/rootfs \
    BSWORLD_UML_NIMBLE_BIN=/opt/bsworld/nimble-linux \
    BSWORLD_UML_ZEPHYR_BIN=/opt/bsworld/zephyr-shell \
    BSWORLD_UML_NUTTX_BIN=/opt/bsworld/nuttx-btsak \
    BSWORLD_UML_NUTTX_NIMBLE_BIN=/opt/bsworld/nuttx-nimble \
    BSWORLD_UML_TROUBLE_CENTRAL_BIN=/opt/bsworld/trouble/ble_bas_central \
    BSWORLD_UML_TROUBLE_PERIPHERAL_BIN=/opt/bsworld/trouble/ble_bas_peripheral
RUN bsworld-check-binaries "$BSWORLD_UML_KERNEL" \
    && test -x "$BSWORLD_UML_ROOTFS/init" \
    && file -b "$BSWORLD_UML_GETRANDOM" | grep -q '^ELF 32-bit' \
    && for binary in "$BSWORLD_UML_KERNEL" \
            "$BSWORLD_UML_ROOTFS/usr/bin/btattach" \
            "$BSWORLD_UML_ROOTFS/usr/bin/bluetoothctl" \
            "$BSWORLD_UML_ROOTFS/usr/bin/dbus-daemon" \
            "$BSWORLD_UML_ROOTFS/usr/libexec/bluetooth/bluetoothd" \
            "$BSWORLD_UML_ROOTFS$BSWORLD_UML_NIMBLE_BIN" \
            "$BSWORLD_UML_ROOTFS$BSWORLD_UML_ZEPHYR_BIN" \
            "$BSWORLD_UML_ROOTFS$BSWORLD_UML_NUTTX_BIN" \
            "$BSWORLD_UML_ROOTFS$BSWORLD_UML_NUTTX_NIMBLE_BIN" \
            "$BSWORLD_UML_ROOTFS$BSWORLD_UML_TROUBLE_CENTRAL_BIN" \
            "$BSWORLD_UML_ROOTFS$BSWORLD_UML_TROUBLE_PERIPHERAL_BIN"; do \
        if ! test -x "$binary" || ! file -b "$binary" | grep -q '^ELF 32-bit'; then \
            printf 'Expected an executable ELF 32-bit file at %s\n' "$binary" >&2; \
            exit 1; \
        fi; \
    done
CMD ["pytest", "tests/integration", \
     "--require-integration", "-n", "auto", "--out-dir", "/output", "--junitxml=/output/pytest.xml"]
