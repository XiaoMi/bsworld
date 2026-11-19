# syntax=docker/dockerfile:1

# Zephyr image with the Bluetooth Host shell. Build with docker/docker-bake.hcl,
# which links the babblesim and runtime-base stages from base.Dockerfile.

# Zephyr: follow the official Getting Started Guide for Ubuntu 24.04.
# https://docs.zephyrproject.org/latest/develop/getting_started/index.html
FROM babblesim AS zephyr-workspace
# Build arguments do not carry over from base.Dockerfile.
ARG BUILD_UID=1000
ARG BUILD_GID=1000
USER root

# git, file, make, GCC/multilib, Python headers, and venv support are inherited above.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cmake ninja-build gperf dfu-util device-tree-compiler \
    libmagic1 libsdl2-dev python3-tk \
    wget unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN install -d -o "$BUILD_UID" -g "$BUILD_GID" /opt/zephyrproject /opt/zephyr-sdk
USER bsworld

# Match the typed-address and periodic-sync command interfaces used by the shell.
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
# Fetch only the modules the nrf52_bsim Bluetooth shell uses, without history:
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

# Copy only the build inputs, so editing a test script keeps the shell layer cached.
FROM zephyr-workspace AS zephyr-shell
ARG BUILD_JOBS=4
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/zephyr/shell/source /opt/bsworld-build/zephyr/shell
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && export CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell" \
        "$ZEPHYR_BASE/tests/bluetooth/shell" \
        -- -DCONF_FILE=/opt/bsworld-build/zephyr/shell/prj.conf \
        -DDTC_OVERLAY_FILE="$ZEPHYR_BASE/tests/bluetooth/shell/xterm-native-shell.overlay;/opt/bsworld-build/zephyr/shell/bsim_host_only.overlay" \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe"

# The image runs its suites by default.
FROM runtime-base AS runtime-zephyr
COPY --from=zephyr-shell /opt/zephyrproject/build/bsworld_zephyr_shell/zephyr/zephyr.exe /opt/firmware/zephyr/shell
ENV BSWORLD_ZEPHYR_SHELL_BIN=/opt/firmware/zephyr/shell
RUN bsworld-check-binaries "$BSWORLD_ZEPHYR_SHELL_BIN"
CMD ["pytest", "tests/integration/zephyr", "-n", "auto", "--out-dir", "/output", "--junitxml=/output/pytest.xml"]
