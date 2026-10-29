# syntax=docker/dockerfile:1

# Build independent Linux/x86_64 firmware stages from a stable tool environment.
# The final runtime stage contains pytest, firmware, and runtime libraries.
# Build stages can be selected with --target when developing firmware.

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

# BSWorld: only this branch depends on the project's Python dependencies.
# Firmware builds reuse build-base even when pyproject.toml changes.
FROM build-base AS bsworld
COPY pyproject.toml /opt/bsworld-environment/pyproject.toml
RUN python -c 'import subprocess, tomllib; \
    config = tomllib.load(open("/opt/bsworld-environment/pyproject.toml", "rb")); \
    dependencies = config["build-system"]["requires"] + config["project"]["dependencies"]; \
    subprocess.check_call(["python", "-m", "pip", "install", *dependencies])'

# Install the project once for early Python checks and the final runtime image.
# Keep this branch separate from firmware so Python changes reuse compiled outputs.
FROM bsworld AS python-test
ENV PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1 \
    BSWORLD_ROOT=/workspace
COPY --chown=$BUILD_UID:$BUILD_GID pyproject.toml /workspace/pyproject.toml
COPY --chown=$BUILD_UID:$BUILD_GID src /workspace/src
# BSController is bundled by the BSWorld distribution and installed with it.
COPY --chown=$BUILD_UID:$BUILD_GID components/bscontroller /workspace/components/bscontroller
COPY --chown=$BUILD_UID:$BUILD_GID tests /workspace/tests
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/bsworld/
# Git metadata is excluded from images; CI supplies the Git-derived version.
ARG BSWORLD_VERSION=0+local
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSWORLD="$BSWORLD_VERSION" \
    python -m pip install --no-deps --no-build-isolation -e .

# BabbleSim: native Linux compiler, linker, and Make-based build support.
FROM build-base AS babblesim
USER root

# Install multilib support before BabbleSim builds, as required by the README setup.
# binutils supplies ar/nm; the Makefiles also use standard file and text utilities.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    repo binutils make gcc g++ gcc-multilib g++-multilib \
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

# Zephyr: follow the official Getting Started Guide for Ubuntu 24.04.
# https://docs.zephyrproject.org/latest/develop/getting_started/index.html
FROM babblesim AS zephyr
USER root

# git, file, make, GCC/multilib, Python headers, and venv support are inherited above.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cmake ninja-build gperf dfu-util device-tree-compiler \
    libmagic1 libsdl2-dev python3-tk \
    wget unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/zephyrproject /opt/zephyr-sdk
USER bsworld

# Match the typed-address and periodic-sync command interfaces used by the shells.
ARG ZEPHYR_REVISION=f545c1ca45bd0e5cb767d01996f5806f9f1e50be

# The workspace lives outside /workspace so mounting the BSWorld checkout keeps it available.
ENV ZEPHYR_WORKSPACE=/opt/zephyrproject \
    ZEPHYR_BASE=/opt/zephyrproject/zephyr \
    ZEPHYR_SDK_INSTALL_DIR=/opt/zephyr-sdk

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
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west update \
    && west packages pip --install \
    && west zephyr-export

# The SDK version is selected from the pinned checkout's SDK_VERSION file.
# Install the x86 toolchain used by native simulation; override this argument
# with a space-separated list when adding targets for other architectures.
ARG ZEPHYR_SDK_TOOLCHAINS=x86_64-zephyr-elf
# --install-base keeps west's temporary extraction directory in the writable SDK base.
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && cd "$ZEPHYR_BASE" \
    && west sdk install --install-base "$ZEPHYR_SDK_INSTALL_DIR" \
        --gnu-toolchains ${ZEPHYR_SDK_TOOLCHAINS}

# Compile the upstream hci_virt Controller and Host-only shells.
COPY --chown=$BUILD_UID:$BUILD_GID components/hci_virt_overlay.conf /opt/bsworld-build/hci_virt_overlay.conf
ARG BUILD_JOBS=4
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && export CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller" \
        "$ZEPHYR_BASE/samples/bluetooth/hci_virt" \
        -- -DEXTRA_CONF_FILE=/opt/bsworld-build/hci_virt_overlay.conf \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller/zephyr/zephyr.exe"
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/zephyr/shell/source /opt/bsworld-build/zephyr/shell
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && export CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell" \
        "$ZEPHYR_BASE/tests/bluetooth/shell" \
        -- -DCONF_FILE=/opt/bsworld-build/zephyr/shell/prj.conf \
        -DEXTRA_CONF_FILE=/opt/bsworld-build/zephyr/shell/le.conf \
        -DDTC_OVERLAY_FILE="$ZEPHYR_BASE/tests/bluetooth/shell/xterm-native-shell.overlay;/opt/bsworld-build/zephyr/shell/bsim_host_only.overlay" \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_mesh_shell" \
        "$ZEPHYR_BASE/tests/bluetooth/mesh_shell" \
        -- -DCONF_FILE=/opt/bsworld-build/zephyr/shell/prj.conf \
        -DEXTRA_CONF_FILE=/opt/bsworld-build/zephyr/shell/mesh.conf \
        -DDTC_OVERLAY_FILE="$ZEPHYR_BASE/tests/bluetooth/shell/xterm-native-shell.overlay;/opt/bsworld-build/zephyr/shell/bsim_host_only.overlay" \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_mesh_shell/zephyr/zephyr.exe"

# NuttX: prepare tools and source workspaces for sim:bthcisock and sim:nimble.
# https://nuttx.apache.org/docs/latest/quickstart/install.html
FROM babblesim AS nuttx
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

# QEMU: libraries and Python tooling for the BSWorld aarch64-softmmu fork.
FROM babblesim AS qemu
USER root

# Keep the optional libraries previously inherited from Zephyr and NuttX.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ninja-build pkg-config bison flex \
    libfdt-dev libffi-dev libglib2.0-dev libpixman-1-dev zlib1g-dev \
    libexpat1-dev libelf-dev libgmp-dev libncurses-dev libsdl2-dev \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/qemu
USER bsworld

# The pinned QEMU pythondeps.toml requires Meson >= 1.6.0 and pycotap >= 1.1.0.
# QEMU configure may install additional dependencies declared by that checkout.
RUN python -m pip install wheel "meson>=1.6.0" "pycotap>=1.1.0"

# Use the validated revision of BSWorld's BabbleSim-aware QEMU fork.
ARG QEMU_REVISION=b2d1776bca209773c4a023e4decb785f7dc3cfb4
ENV QEMU_DIR=/opt/qemu

RUN git init "$QEMU_DIR" \
    && git -C "$QEMU_DIR" remote add origin https://github.com/LingaoM/qemu.git \
    && git -C "$QEMU_DIR" fetch --progress --depth=1 origin "$QEMU_REVISION" \
    && git -C "$QEMU_DIR" checkout --detach FETCH_HEAD \
    && test "$(git -C "$QEMU_DIR" rev-parse HEAD)" = "$QEMU_REVISION"

ARG BUILD_JOBS=4
RUN mkdir -p "$QEMU_DIR/build/bsworld" \
    && cd "$QEMU_DIR/build/bsworld" \
    && "$QEMU_DIR/configure" --target-list=aarch64-softmmu --enable-bsim \
        --bsim-components-path="$BSIM_COMPONENTS_PATH" \
        --bsim-out-path="$BSIM_OUT_PATH" \
    && ninja -j"$BUILD_JOBS" qemu-system-aarch64 \
    && strip --strip-unneeded qemu-system-aarch64

# Buildroot: host utilities for the toolchain, Linux kernel, and guest root filesystem.
# Guest kernel/rootfs builds do not depend on QEMU or BabbleSim.
FROM build-base AS buildroot
USER root

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bc bzip2 cpio debianutils diffutils gzip \
    patch perl rsync tar zip libssl-dev \
    bison flex gawk wget unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/buildroot
USER bsworld

# Validated Buildroot revision for the maintained Linux guest configuration.
ARG BUILDROOT_REVISION=1ad0796f2b40eb8dadc43e95dd1d56708b191375
ENV BUILDROOT_DIR=/opt/buildroot \
    BR2_DL_DIR=/cache/downloads/buildroot

RUN git init "$BUILDROOT_DIR" \
    && git -C "$BUILDROOT_DIR" remote add origin https://gitlab.com/buildroot.org/buildroot.git \
    && git -C "$BUILDROOT_DIR" fetch --progress --depth=1 origin "$BUILDROOT_REVISION" \
    && git -C "$BUILDROOT_DIR" checkout --detach FETCH_HEAD \
    && test "$(git -C "$BUILDROOT_DIR" rev-parse HEAD)" = "$BUILDROOT_REVISION"

# Build the AArch64 kernel and initramfs with the shared Linux guest packages.
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/qemu/source /opt/bsworld-build/qemu-guest
ARG BUILD_JOBS=4
# Share downloads across configuration changes without reusing a stale output tree.
# Include the user IDs so local builds with different owners have writable caches.
RUN --mount=type=cache,id=bsworld-buildroot-downloads-${BUILD_UID}-${BUILD_GID},target=/cache/downloads/buildroot,uid=${BUILD_UID},gid=${BUILD_GID},sharing=locked \
    make -C "$BUILDROOT_DIR" O="$BUILDROOT_DIR/output-bsworld-linux" \
        BR2_EXTERNAL=/opt/bsworld-build/qemu-guest bsworld_qemu_linux_defconfig \
    && make -C "$BUILDROOT_DIR" O="$BUILDROOT_DIR/output-bsworld-linux" -j"$BUILD_JOBS" \
    && test -f "$BUILDROOT_DIR/output-bsworld-linux/images/Image" \
    && test -f "$BUILDROOT_DIR/output-bsworld-linux/images/rootfs.cpio" \
    && test -x "$BUILDROOT_DIR/output-bsworld-linux/target/usr/bin/btattach" \
    && test -x "$BUILDROOT_DIR/output-bsworld-linux/target/usr/bin/nimble-linux"

# TrouBLE: build the native adapter against its pinned upstream Host commit.
# The adapter joins the BabbleSim runtime directly and is kept alongside the
# Zephyr, NuttX, and QEMU branches rooted in the common BabbleSim stage.
FROM babblesim AS trouble
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

# Extend the Python image that passed lint/unit tests with runtime libraries and
# firmware. BabbleSim is copied whole; other components' source/SDKs stay above.
FROM python-test AS runtime
USER root
# These are the runtime counterparts of the firmware stages' development packages.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    procps \
    libc6-i386 lib32gcc-s1 lib32stdc++6 lib32z1 libstdc++6 \
    libglib2.0-0t64 libpixman-1-0 libfdt1 libffi8 zlib1g libsndio7.0 \
    libsdl2-2.0-0 libx11-6 libpulse0 \
    libexpat1 libelf1t64 libgmp10 libncursesw6 libtinfo6 \
    && rm -rf /var/lib/apt/lists/*
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/babblesim/build/results

# Keep BabbleSim's source, scripts, and outputs together at their build-time paths.
COPY --from=babblesim --chown=$BUILD_UID:$BUILD_GID /opt/babblesim /opt/babblesim
COPY --from=zephyr /opt/zephyrproject/build/bsworld_bt_virt_controller/zephyr/zephyr.exe /opt/firmware/bt_virt_controller
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_shell/zephyr/zephyr.exe /opt/firmware/zephyr/shell
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_mesh_shell/zephyr/zephyr.exe /opt/firmware/zephyr/mesh_shell
COPY --from=nuttx /opt/nuttx/nuttx /opt/firmware/nuttx/btsak
COPY --from=nuttx /opt/nuttx-nimble/nuttx /opt/firmware/nuttx/nimble
COPY --from=qemu /opt/qemu/build/bsworld/qemu-system-aarch64 /opt/firmware/qemu/qemu-system-aarch64
COPY --from=buildroot /opt/buildroot/output-bsworld-linux/images/Image /opt/firmware/qemu/Image
COPY --from=buildroot /opt/buildroot/output-bsworld-linux/images/rootfs.cpio /opt/firmware/qemu/rootfs.cpio
COPY --from=trouble /opt/trouble/target/release/ble_bas /opt/firmware/trouble/ble_bas

ENV BSIM_OUT_PATH=/opt/babblesim/build \
    BSIM_COMPONENTS_PATH=/opt/babblesim/components \
    BSWORLD_BT_VIRT_CONTROLLER_BIN=/opt/firmware/bt_virt_controller \
    BSWORLD_ZEPHYR_SHELL_BIN=/opt/firmware/zephyr/shell \
    BSWORLD_ZEPHYR_MESH_SHELL_BIN=/opt/firmware/zephyr/mesh_shell \
    BSWORLD_NUTTX_BTSAK_BIN=/opt/firmware/nuttx/btsak \
    BSWORLD_NUTTX_NIMBLE_BIN=/opt/firmware/nuttx/nimble \
    BSWORLD_QEMU_BIN=/opt/firmware/qemu/qemu-system-aarch64 \
    BSWORLD_QEMU_KERNEL=/opt/firmware/qemu/Image \
    BSWORLD_QEMU_ROOTFS=/opt/firmware/qemu/rootfs.cpio \
    BSWORLD_TROUBLE_NATIVE_BIN=/opt/firmware/trouble/ble_bas

USER bsworld

# Verify binaries and their dynamic dependencies in the final filesystem.
# BabbleSim .Tsymbols files are readable text maps, not executable binaries.
RUN for binary in /opt/firmware/bt_virt_controller /opt/firmware/zephyr/* \
        /opt/firmware/nuttx/* /opt/firmware/qemu/qemu-system-aarch64 \
        /opt/firmware/trouble/ble_bas \
        /opt/babblesim/build/bin/*; do \
        case "$binary" in *.Tsymbols) continue ;; esac; \
        test -f "$binary" && test -x "$binary" || { \
            printf 'Missing or non-executable binary: %s\n' "$binary"; exit 1; }; \
        dependencies="$(ldd "$binary" 2>&1)"; status=$?; \
        case "$dependencies" in *"not a dynamic executable"*|*"statically linked"*) ;; \
            *) test "$status" -eq 0 || { printf 'ldd failed: %s\n%s\n' "$binary" "$dependencies"; exit 1; } ;; esac; \
        if printf '%s\n' "$dependencies" | grep -q 'not found'; then \
            printf 'Missing runtime dependencies: %s\n%s\n' "$binary" "$dependencies"; exit 1; \
        fi; \
    done

CMD ["pytest", "tests/integration", "-n", "2", "--out-dir", "/output", "--junitxml=/output/pytest.xml"]
