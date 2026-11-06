# syntax=docker/dockerfile:1

# Zephyr image: the Bluetooth shells on the shared Zephyr workspace. The
# zephyr-workspace and runtime-base stages come from base.Dockerfile; build
# this file with docker/docker-bake.hcl, which links them.

# Copy only the build inputs, so editing a test script keeps the shell layer cached.
# Complete firmware joins the PHY with its own Controller, without an HCI socket.
FROM zephyr-workspace AS zephyr-shell
# Build arguments do not carry over from base.Dockerfile.
ARG BUILD_UID=1000
ARG BUILD_GID=1000
ARG BUILD_JOBS=4
COPY --chown=$BUILD_UID:$BUILD_GID tests/integration/zephyr/shell/source /opt/bsworld-build/zephyr/shell
RUN . "$ZEPHYR_WORKSPACE/.venv/bin/activate" \
    && export CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS" \
    && cd "$ZEPHYR_WORKSPACE" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_full_shell" \
        "$ZEPHYR_BASE/tests/bluetooth/shell" \
        -- -DCONF_FILE=/opt/bsworld-build/zephyr/shell/prj.conf \
        -DEXTRA_CONF_FILE="/opt/bsworld-build/zephyr/shell/le.conf;/opt/bsworld-build/zephyr/shell/full.conf" \
        -DDTC_OVERLAY_FILE="$ZEPHYR_BASE/tests/bluetooth/shell/xterm-native-shell.overlay" \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_full_shell/zephyr/zephyr.exe" \
    && west build -p always -b nrf52_bsim \
        -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell" \
        "$ZEPHYR_BASE/tests/bluetooth/shell" \
        -- -DCONF_FILE=/opt/bsworld-build/zephyr/shell/prj.conf \
        -DEXTRA_CONF_FILE=/opt/bsworld-build/zephyr/shell/le.conf \
        -DDTC_OVERLAY_FILE="$ZEPHYR_BASE/tests/bluetooth/shell/xterm-native-shell.overlay;/opt/bsworld-build/zephyr/shell/bsim_host_only.overlay" \
    && strip --strip-unneeded "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_shell/zephyr/zephyr.exe"

# The image runs its suites by default, and also the Bumble and BabbleSim
# suites, which need no firmware of their own.
FROM runtime-base AS runtime-zephyr
COPY --from=zephyr-shell /opt/zephyrproject/build/bsworld_zephyr_full_shell/zephyr/zephyr.exe /opt/firmware/zephyr/full_shell
COPY --from=zephyr-shell /opt/zephyrproject/build/bsworld_zephyr_shell/zephyr/zephyr.exe /opt/firmware/zephyr/shell
ENV BSWORLD_ZEPHYR_FULL_SHELL_BIN=/opt/firmware/zephyr/full_shell \
    BSWORLD_ZEPHYR_SHELL_BIN=/opt/firmware/zephyr/shell
RUN bsworld-check-binaries /opt/firmware/zephyr/full_shell /opt/firmware/zephyr/shell
CMD ["pytest", "tests/integration/zephyr", "tests/integration/bumble", "tests/integration/babblesim", "-n", "auto", "--out-dir", "/output", "--junitxml=/output/pytest.xml"]
