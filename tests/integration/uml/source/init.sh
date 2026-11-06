#!/bin/sh
# Copyright 2026 Xiaomi Corporation
#
# SPDX-License-Identifier: Apache-2.0
#
# PID 1 of the UML guest. The root file system is the host's, mounted
# read-only, so every path the guest writes lives in guest memory.

set -e
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
# No terminal control sequences, such as readline's bracketed-paste mode, so
# console lines start with their text.
export TERM=dumb

mount -t proc proc /proc
mount -t sysfs sysfs /sys
for dir in /tmp /run /var/lib/bluetooth; do
	mount -t tmpfs tmpfs "$dir"
done

mkdir /run/dbus
dbus-daemon --system --fork

bluetoothd=
for candidate in /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd; do
	if [ -x "$candidate" ]; then
		bluetoothd=$candidate
		break
	fi
done
if [ -z "$bluetoothd" ]; then
	# Exiting PID 1 panics the guest, so the reason ends the console log.
	echo "init.sh: no bluetoothd in /usr/libexec/bluetooth or /usr/lib/bluetooth" >&2
	exit 1
fi
"$bluetoothd" --nodetach --configfile="$(dirname "$0")/main.conf" &

echo BSWORLD_UML_READY
exec /bin/sh
