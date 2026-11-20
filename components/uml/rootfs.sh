#!/bin/sh
# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

set -eu
rootfs=$1
packages=$(mktemp -d)
trap 'rm -rf "$packages"' EXIT
touch "$packages/status"

# Resolve the dependencies for an empty i386 system, then unpack the debs.
# This keeps the host's installed packages and the guest filesystem separate.
apt-get -o APT::Architecture=i386 \
  -o Dir::State::status="$packages/status" -o Dir::Cache::archives="$packages" \
  --download-only --no-install-recommends -y install \
  dash coreutils mount bluez dbus-daemon \
  libmbedcrypto7t64 libstdc++6 zlib1g

mkdir -p "$rootfs"
for package in "$packages"/*.deb; do
    case "$(dpkg-deb -f "$package" Architecture)" in
        i386|all) dpkg-deb -x "$package" "$rootfs" ;;
        *) printf 'Unexpected guest package architecture for %s\n' "$package" >&2; exit 1 ;;
    esac
done

# Guest programs run as root under UML. Preserve this identity even when the
# package archive was extracted by an unprivileged host user.
find "$rootfs" -type f -perm /6000 -exec chmod a-s {} +

# Packages can still contain files under /lib. Merge the paths as Ubuntu's
# base installation does so the i386 loader is reachable at /lib/ld-linux.so.2.
for directory in bin sbin lib; do
    if [ -d "$rootfs/$directory" ] && [ ! -L "$rootfs/$directory" ]; then
        cp -a "$rootfs/$directory/." "$rootfs/usr/$directory/"
        rm -rf "$rootfs/$directory"
    fi
    ln -sfn "usr/$directory" "$rootfs/$directory"
done

# Extracting the packages skips their setup scripts. Create /bin/sh for /init.
ln -sfn dash "$rootfs/usr/bin/sh"

# Prepare the Guest and BlueZ mount points and the Host program directory.
mkdir -p "$rootfs/dev" "$rootfs/proc" "$rootfs/sys" "$rootfs/tmp" \
  "$rootfs/run" "$rootfs/var/lib/bluetooth" "$rootfs/opt/bsworld"

# The system D-Bus daemon starts as root and switches to messagebus.
printf 'root:x:0:0:root:/root:/bin/sh\nmessagebus:x:100:100::/nonexistent:/usr/sbin/nologin\n' \
  > "$rootfs/etc/passwd"
printf 'root:x:0:\nmessagebus:x:100:\n' > "$rootfs/etc/group"

# Supply the fixed machine ID used by the Guest's system bus.
printf '00000000000000000000000000000001\n' > "$rootfs/etc/machine-id"
