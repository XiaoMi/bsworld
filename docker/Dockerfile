# Compile Linux/x86_64 simulation firmware in component order.
# The final runtime stage contains pytest, firmware, and runtime libraries.
# Build stages can be selected with --target when developing firmware.

# BSWorld: create the Python environment and install declared project dependencies.
FROM public.ecr.aws/docker/library/ubuntu:24.04 AS bsworld

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
ARG BUILD_JOBS=4
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
    CMAKE_BUILD_PARALLEL_LEVEL=$BUILD_JOBS \
    PIP_CACHE_DIR=/cache/pip \
    BR2_DL_DIR=/cache/downloads/buildroot

# Install Python dependencies before firmware compilation so test-only changes
# can reuse the firmware layers. Install the BSWorld package in the final stage.
COPY pyproject.toml /opt/bsworld-environment/pyproject.toml
RUN python -c 'import subprocess, tomllib; \
    config = tomllib.load(open("/opt/bsworld-environment/pyproject.toml", "rb")); \
    dependencies = config["build-system"]["requires"] + config["project"]["dependencies"]; \
    subprocess.check_call(["python", "-m", "pip", "install", *dependencies])'

WORKDIR /workspace
CMD ["bash"]

# BabbleSim: native Linux compiler, linker, and Make-based build support.
FROM bsworld AS babblesim
USER root

# Install multilib support before BabbleSim builds, as required by the README setup.
# binutils supplies ar/nm; the Makefiles also use standard file and text utilities.
# GNU awk is needed for the default Ellisys trace conversion after simulation.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    binutils make gcc g++ gcc-multilib g++-multilib \
    coreutils findutils grep sed gawk \
    && rm -rf /var/lib/apt/lists/*

USER bsworld

# These revisions match the BabbleSim submodules in the BSWorld checkout.
# Fetch them in Docker so building the image also works before local submodule setup.
ARG BABBLESIM_REVISIONS="base:fd65faae944f7ea082163b3c663067d978d7d1b9 \
    ext_2G4_channel_NtNcable:20a38c997f507b0aa53817aab3d73a462fff7af1 \
    ext_2G4_channel_multiatt:bde72a57384dde7a4310bcf3843469401be93074 \
    ext_2G4_device_WLAN_actmod:9cb6d8e72695f6b785e57443f0629a18069d6ce4 \
    ext_2G4_device_burst_interferer:5b5339351d6e6a2368c686c734dc8b2fc65698fc \
    ext_2G4_device_playback:abb48cd71ddd4e2a9022f4bf49b2712524c483e8 \
    ext_2G4_device_playbackv2:0a3c28ecd59b5ee08ed4668446c243d3ffd98b46 \
    ext_2G4_libPhyComv1:15ae0f87fa049e04cbec48a866f3bc37d903f950 \
    ext_2G4_modem_BLE_simple:4d2379de510684cd4b1c3bbbb09bce7b5a20bc1f \
    ext_2G4_modem_magic:edfcda2d3937a74be0a59d6cd47e0f50183453da \
    ext_2G4_phy_v1:62e797b2c518e5bb6123a198382ed2b64b8c068e"
ENV BSIM_OUT_PATH=/opt/babblesim/build \
    BSIM_COMPONENTS_PATH=/opt/babblesim/components \
    BSIM_BASE_PATH=/opt/babblesim/components \
    BSIM_BUILD_FAIL_ASAP=1
USER root
RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/babblesim
USER bsworld
RUN mkdir -p "$BSIM_COMPONENTS_PATH" \
    && for entry in $BABBLESIM_REVISIONS; do \
        component="${entry%%:*}"; revision="${entry#*:}"; \
        source_dir="/opt/babblesim/$component"; \
        git init "$source_dir" \
        && git -C "$source_dir" remote add origin "https://github.com/BabbleSim/$component.git" \
        && git -C "$source_dir" fetch --progress --depth=1 origin "$revision" \
        && git -C "$source_dir" checkout --detach FETCH_HEAD \
        && git -C "$source_dir" submodule update --init --recursive \
        && test "$(git -C "$source_dir" rev-parse HEAD)" = "$revision" || exit 1; \
        if [ "$component" != base ]; then \
            ln -s "../$component" "$BSIM_COMPONENTS_PATH/$component" || exit 1; \
        fi; \
    done \
    && for component in common libPhyComv1 libRandv2 libUtilv1 \
        device_empty device_handbrake device_pause_simu device_time_monitor; do \
        ln -s "../base/$component" "$BSIM_COMPONENTS_PATH/$component" || exit 1; \
    done \
    && ln -s components/common/Makefile /opt/babblesim/Makefile
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

# Validated Zephyr revision for the Controller and Host-only sample configurations.
ARG ZEPHYR_REVISION=35c46048d330ce1bea9248d4dd6a9879d0fe5be7

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

# Compile the BSWorld Controller and upstream Host-only Bluetooth samples.
COPY --chown=$BUILD_UID:$BUILD_GID components/bt_virt_controller /opt/bsworld-build/bt_virt_controller
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller" \
        /opt/bsworld-build/bt_virt_controller \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller/zephyr/zephyr.exe"
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/zephyr/samples/common/source /opt/bsworld-build/zephyr/common
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/zephyr/samples/mesh/source /opt/bsworld-build/zephyr/mesh
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && cd "$ZEPHYR_WORKSPACE" \
    && for sample in broadcaster observer peripheral_hr central_hr \
        periodic_adv periodic_sync mesh mesh_provisioner; do \
        extra_conf=/opt/bsworld-build/zephyr/common/bsim.conf; \
        case "$sample" in mesh|mesh_provisioner) \
            extra_conf="$extra_conf;/opt/bsworld-build/zephyr/mesh/mesh.conf" ;; esac; \
        west build -p always -b nrf52_bsim \
            -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_$sample" \
            "$ZEPHYR_BASE/samples/bluetooth/$sample" \
            -- -DEXTRA_CONF_FILE="$extra_conf" \
            -DDTC_OVERLAY_FILE=/opt/bsworld-build/zephyr/common/bsim_host_only.overlay \
        && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_$sample/zephyr/zephyr.exe" \
        || exit 1; \
    done

# NuttX: prepare tools and source workspaces for sim:bthcisock and sim:nimble.
# https://nuttx.apache.org/docs/latest/quickstart/install.html
FROM zephyr AS nuttx
USER root

# kconfig-frontends provides kconfig-tweak for the documented test configurations.
# The simulator uses the inherited host/multilib compiler rather than an ARM toolchain.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    autoconf automake bison flex gettext texinfo gperf libtool pkg-config \
    libncurses-dev xxd genromfs kconfig-frontends \
    libgmp-dev libmpc-dev libmpfr-dev libisl-dev binutils-dev libelf-dev \
    libexpat1-dev picocom u-boot-tools util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" \
    /opt/nuttx /opt/nuttx-apps /opt/nuttx-nimble /opt/nuttx-nimble-apps
USER bsworld

# Install Kconfiglib in the active virtual environment so Python can import it.
RUN python -m pip install kconfiglib

# Validated NuttX and apps revisions for both simulator test configurations.
# Fetch exact commits to avoid downloading the entire upstream Git history.
ARG NUTTX_REVISION=89c4b8ccaf03f1c6d33624d93ef640d8d176a78d
ARG NUTTX_APPS_REVISION=8404450fb89346ed244d1c701e5feb5d19f32271
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
RUN cd "$NUTTX_DIR" \
    && ./tools/configure.sh -l -a ../nuttx-apps sim:bthcisock \
    && kconfig-tweak --file .config -e SIM_BSIM_TIME -d BLUETOOTH_SMP_SELFTEST \
    && make olddefconfig \
    && make -j"$BUILD_JOBS" \
    && strip --strip-unneeded nuttx

# sim:nimble enables 32-bit compilation and FAT, which links against host zlib.
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    lib32z1-dev \
    && rm -rf /var/lib/apt/lists/*
USER bsworld
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
FROM nuttx AS qemu
USER root

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libfdt-dev libffi-dev libglib2.0-dev libpixman-1-dev zlib1g-dev \
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

RUN mkdir -p "$QEMU_DIR/build/bsworld" \
    && cd "$QEMU_DIR/build/bsworld" \
    && "$QEMU_DIR/configure" --target-list=aarch64-softmmu --enable-bsim \
        --bsim-components-path="$BSIM_COMPONENTS_PATH" \
        --bsim-out-path="$BSIM_OUT_PATH" \
    && ninja -j"$BUILD_JOBS" qemu-system-aarch64 \
    && strip --strip-unneeded qemu-system-aarch64

# Buildroot: host utilities for the toolchain, Linux kernel, and guest root filesystem.
FROM qemu AS buildroot
USER root

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bc bzip2 cpio debianutils diffutils gzip \
    patch perl rsync tar zip libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/buildroot
USER bsworld

# Validated Buildroot revision for the maintained Linux guest configuration.
ARG BUILDROOT_REVISION=1ad0796f2b40eb8dadc43e95dd1d56708b191375
ENV BUILDROOT_DIR=/opt/buildroot

RUN git init "$BUILDROOT_DIR" \
    && git -C "$BUILDROOT_DIR" remote add origin https://gitlab.com/buildroot.org/buildroot.git \
    && git -C "$BUILDROOT_DIR" fetch --progress --depth=1 origin "$BUILDROOT_REVISION" \
    && git -C "$BUILDROOT_DIR" checkout --detach FETCH_HEAD \
    && test "$(git -C "$BUILDROOT_DIR" rev-parse HEAD)" = "$BUILDROOT_REVISION"

# Build the AArch64 kernel and initramfs with the shared Linux guest packages.
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/qemu/source /opt/bsworld-build/qemu-guest
RUN make -C "$BUILDROOT_DIR" O="$BUILDROOT_DIR/output-bsworld-linux" \
        BR2_EXTERNAL=/opt/bsworld-build/qemu-guest bsworld_qemu_linux_defconfig \
    && make -C "$BUILDROOT_DIR" O="$BUILDROOT_DIR/output-bsworld-linux" -j"$BUILD_JOBS" \
    && test -f "$BUILDROOT_DIR/output-bsworld-linux/images/Image" \
    && test -f "$BUILDROOT_DIR/output-bsworld-linux/images/rootfs.cpio" \
    && test -x "$BUILDROOT_DIR/output-bsworld-linux/target/usr/bin/btattach" \
    && test -x "$BUILDROOT_DIR/output-bsworld-linux/target/usr/bin/nimble-linux" \
    && test -x "$BUILDROOT_DIR/output-bsworld-linux/target/usr/bin/trouble-ble-bas-peripheral"

# Start from a fresh OS so compiler, SDK, source, and intermediate layers are
# excluded from the published runtime image.
FROM public.ecr.aws/docker/library/ubuntu:24.04 AS runtime
ARG BUILD_UID=1000
ARG BUILD_GID=1000
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash ca-certificates python3.12 procps gawk \
    libc6-i386 lib32gcc-s1 lib32stdc++6 lib32z1 libstdc++6 \
    libglib2.0-0t64 libpixman-1-0 libfdt1 libffi8 zlib1g libsndio7.0 \
    libsdl2-2.0-0 libx11-6 libpulse0 \
    && rm -rf /var/lib/apt/lists/*
RUN if ! getent group "$BUILD_GID" >/dev/null; then groupadd -g "$BUILD_GID" bsworld; fi \
    && if getent passwd "$BUILD_UID" >/dev/null; then \
        build_user="$(getent passwd "$BUILD_UID" | cut -d: -f1)"; \
        usermod -l bsworld -d /home/bsworld -m -g "$BUILD_GID" "$build_user"; \
    else useradd -m -u "$BUILD_UID" -g "$BUILD_GID" -s /bin/bash bsworld; fi \
    && install -d -o "$BUILD_UID" -g "$BUILD_GID" /workspace /output /opt/babblesim/build/results

# Preserve BabbleSim's bin/../lib layout and NuttX's build-time library rpath.
COPY --from=babblesim --chown=$BUILD_UID:$BUILD_GID /opt/babblesim/build/bin /opt/babblesim/build/bin
COPY --from=babblesim --chown=$BUILD_UID:$BUILD_GID /opt/babblesim/build/lib/*.so /opt/babblesim/build/lib/
COPY --from=babblesim --chown=$BUILD_UID:$BUILD_GID /opt/babblesim/ext_2G4_phy_v1/dump_post_process/convert_results_to_ellisysv2.sh /opt/babblesim/convert_results_to_ellisysv2.sh
COPY --from=zephyr /opt/zephyrproject/build/bsworld_bt_virt_controller/zephyr/zephyr.exe /opt/firmware/bt_virt_controller
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_broadcaster/zephyr/zephyr.exe /opt/firmware/zephyr/broadcaster
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_observer/zephyr/zephyr.exe /opt/firmware/zephyr/observer
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_peripheral_hr/zephyr/zephyr.exe /opt/firmware/zephyr/peripheral_hr
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_central_hr/zephyr/zephyr.exe /opt/firmware/zephyr/central_hr
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_periodic_adv/zephyr/zephyr.exe /opt/firmware/zephyr/periodic_adv
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_periodic_sync/zephyr/zephyr.exe /opt/firmware/zephyr/periodic_sync
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_mesh/zephyr/zephyr.exe /opt/firmware/zephyr/mesh
COPY --from=zephyr /opt/zephyrproject/build/bsworld_zephyr_mesh_provisioner/zephyr/zephyr.exe /opt/firmware/zephyr/mesh_provisioner
COPY --from=nuttx /opt/nuttx/nuttx /opt/firmware/nuttx/btsak
COPY --from=nuttx /opt/nuttx-nimble/nuttx /opt/firmware/nuttx/nimble
COPY --from=qemu /opt/qemu/build/bsworld/qemu-system-aarch64 /opt/firmware/qemu/qemu-system-aarch64
COPY --from=buildroot /opt/buildroot/output-bsworld-linux/images/Image /opt/firmware/qemu/Image
COPY --from=buildroot /opt/buildroot/output-bsworld-linux/images/rootfs.cpio /opt/firmware/qemu/rootfs.cpio
COPY --from=bsworld --chown=$BUILD_UID:$BUILD_GID /opt/venv /opt/venv

# Make tests.integration helpers importable through the pytest console command.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/workspace \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 \
    BSWORLD_ROOT=/workspace \
    BSWORLD_BSIM_DIR=/opt/babblesim/build \
    BSWORLD_BSIM_ELLISYS_SCRIPT=/opt/babblesim/convert_results_to_ellisysv2.sh \
    BSWORLD_BT_VIRT_CONTROLLER_BIN=/opt/firmware/bt_virt_controller \
    BSWORLD_ZEPHYR_BROADCASTER_BIN=/opt/firmware/zephyr/broadcaster \
    BSWORLD_ZEPHYR_OBSERVER_BIN=/opt/firmware/zephyr/observer \
    BSWORLD_ZEPHYR_PERIPHERAL_HR_BIN=/opt/firmware/zephyr/peripheral_hr \
    BSWORLD_ZEPHYR_CENTRAL_HR_BIN=/opt/firmware/zephyr/central_hr \
    BSWORLD_ZEPHYR_PERIODIC_ADV_BIN=/opt/firmware/zephyr/periodic_adv \
    BSWORLD_ZEPHYR_PERIODIC_SYNC_BIN=/opt/firmware/zephyr/periodic_sync \
    BSWORLD_ZEPHYR_MESH_BIN=/opt/firmware/zephyr/mesh \
    BSWORLD_ZEPHYR_MESH_PROVISIONER_BIN=/opt/firmware/zephyr/mesh_provisioner \
    BSWORLD_NUTTX_BTSAK_BIN=/opt/firmware/nuttx/btsak \
    BSWORLD_NUTTX_NIMBLE_BIN=/opt/firmware/nuttx/nimble \
    BSWORLD_QEMU_BIN=/opt/firmware/qemu/qemu-system-aarch64 \
    BSWORLD_QEMU_KERNEL=/opt/firmware/qemu/Image \
    BSWORLD_QEMU_ROOTFS=/opt/firmware/qemu/rootfs.cpio

# Editable installation lets CI bind-mount the current checkout at /workspace.
# Firmware remains in /opt while Python source and tests come from that checkout.
COPY --chown=$BUILD_UID:$BUILD_GID pyproject.toml /workspace/pyproject.toml
COPY --chown=$BUILD_UID:$BUILD_GID src /workspace/src
COPY --chown=$BUILD_UID:$BUILD_GID tests /workspace/tests
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/bsworld/
USER bsworld
WORKDIR /workspace
RUN python -m pip install --no-deps --no-build-isolation -e .

# Verify binaries and their dynamic dependencies in the final filesystem.
# BabbleSim .Tsymbols files are readable text maps, not executable binaries.
RUN for binary in /opt/firmware/bt_virt_controller /opt/firmware/zephyr/* \
        /opt/firmware/nuttx/* /opt/firmware/qemu/qemu-system-aarch64 \
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
