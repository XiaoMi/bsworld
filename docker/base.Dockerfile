# syntax=docker/dockerfile:1

# Stages shared by every platform image: build tools, the installed Python
# project, BabbleSim and runtime-base.
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

# Shared base of the platform images: the installed Python project, runtime
# libraries and BabbleSim. BabbleSim is copied whole; other components'
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
ENV BSIM_OUT_PATH=/opt/babblesim/build \
    BSIM_COMPONENTS_PATH=/opt/babblesim/components
USER bsworld
RUN bsworld-check-binaries /opt/babblesim/build/bin/*
