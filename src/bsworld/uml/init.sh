#!/bin/sh
# Copyright 2026 Xiaomi Corporation
#
# SPDX-License-Identifier: Apache-2.0
#
# PID 1 of the UML guest. Provide writable runtime directories on the
# guest's read-only root file system.

set -e
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
# No terminal control sequences, such as readline's bracketed-paste mode, so
# console lines start with their text.
export TERM=dumb

mount -t proc proc /proc
mount -t sysfs sysfs /sys
for dir in /tmp /run; do
	mount -t tmpfs tmpfs "$dir"
done

echo BSWORLD_UML_READY
exec /bin/sh
