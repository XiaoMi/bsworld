# syntax=docker/dockerfile:1

# Stages shared by every platform image: build tools, the installed Python
# project, BabbleSim, the Zephyr workspace and Controller, and runtime-base.
# Each <platform>.Dockerfile builds its image on these; docker/docker-bake.hcl
# links them. The bsworld and python-test targets build from this file alone.

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

# BSWorld: setuptools-scm reads the Git-derived version for local image builds.
FROM build-base AS bsworld
RUN python -m pip install "setuptools-scm>=9.2"

# Install BSWorld from the checkout as README.md describes, for early Python
# checks and the platform images. Keep this branch separate from firmware
# so Python changes reuse compiled outputs.
FROM bsworld AS python-test
ENV PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1 \
    BSWORLD_ROOT=/workspace
# docker/base.Dockerfile.dockerignore selects the files copied from the checkout.
COPY --chown=$BUILD_UID:$BUILD_GID . /workspace/
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/bsworld/
# Git metadata is excluded from images; CI supplies the Git-derived version.
ARG BSWORLD_VERSION=0+local
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSWORLD="$BSWORLD_VERSION" \
    python -m pip install -e components/bscontroller -e '.[test]'

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
FROM babblesim AS zephyr-workspace
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
# Fetch only the modules the nrf52_bsim Bluetooth builds use, without history:
# the full manifest needs about 7.5 GB, more than a hosted CI runner can spare.
ARG ZEPHYR_PROJECT_FILTER=-.*,+cmsis,+cmsis_6,+hal_nordic,+nrf_hw_models,+mbedtls,+tf-psa-crypto,+tf-psa-crypto-drivers,+picolibc
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west config manifest.project-filter -- "$ZEPHYR_PROJECT_FILTER" \
    && west update --narrow --fetch-opt=--depth=1 \
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

# Every platform image needs the hci_virt Controller, but only the Zephyr image
# needs the shells, so each has its own stage on the shared workspace.
FROM zephyr-workspace AS zephyr-controller
ARG BUILD_JOBS=4
COPY --chown=$BUILD_UID:$BUILD_GID components/hci_virt_overlay.conf /opt/bsworld-build/hci_virt_overlay.conf
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && export CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller" \
        "$ZEPHYR_BASE/samples/bluetooth/hci_virt" \
        -- -DEXTRA_CONF_FILE=/opt/bsworld-build/hci_virt_overlay.conf \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller/zephyr/zephyr.exe"

# Shared base of the platform images: the installed Python project, runtime
# libraries, BabbleSim and the Zephyr Controller that every suite's [zephyr]
# cases use. BabbleSim is copied whole; other components'
# source and SDKs stay in their build stages.
FROM python-test AS runtime-base
USER root
# These are the runtime counterparts of the firmware stages' development packages.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    procps \
    libc6-i386 lib32gcc-s1 lib32stdc++6 lib32z1 libstdc++6 zlib1g \
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
COPY --from=zephyr-controller /opt/zephyrproject/build/bsworld_bt_virt_controller/zephyr/zephyr.exe /opt/firmware/zephyr/bt_virt_controller
ENV BSIM_OUT_PATH=/opt/babblesim/build \
    BSIM_COMPONENTS_PATH=/opt/babblesim/components \
    BSWORLD_BT_VIRT_CONTROLLER_BIN=/opt/firmware/zephyr/bt_virt_controller
USER bsworld
RUN bsworld-check-binaries /opt/firmware/zephyr/bt_virt_controller /opt/babblesim/build/bin/*
