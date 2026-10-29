# Third-Party Notices

[English](THIRD_PARTY_NOTICES.md) | [简体中文](docs/zh-CH/THIRD_PARTY_NOTICES.md)

Third-party software used by BSWorld retains its own licenses. Build and configuration details are documented in the linked guides. Component-specific attribution is recorded in [BSController's NOTICE](components/bscontroller/NOTICE).

## BabbleSim

- Source: https://github.com/BabbleSim
- Usage: [shared RF simulation environment](README.md#build-and-test)
- License: Apache License 2.0 unless otherwise stated by an individual component.

BSWorld does not modify the upstream BabbleSim sources. Each component retains its own license files.

## Apache NuttX

- Source: https://github.com/apache/nuttx
- Usage: [NuttX test applications](tests/integration/nuttx/README.md)
- License: Apache License 2.0

NuttX source is obtained externally and is not vendored in this repository.

## Apache NimBLE

- Source: https://github.com/apache/mynewt-nimble
- Usage: Bluetooth Host stack for [NuttX](tests/integration/nuttx/README.md) and [Linux Guest tests](tests/integration/qemu/nimble/README.md)
- License: Apache License 2.0

NimBLE source is obtained externally and is not vendored in this repository.

## TrouBLE

- Source: https://github.com/embassy-rs/trouble
- Usage: Rust Bluetooth Host stack and Battery Service applications for [native Linux tests](tests/integration/trouble/README.md)
- License: MIT or Apache License 2.0

The upstream stack and applications are used without modification as external Cargo dependencies. BSWorld maintains the native transport and executor; the adapter retains its original MIT OR Apache-2.0 license.

## Bumble

- Source: https://github.com/google/bumble
- Usage: Python Bluetooth Host stack for [Bumble tests](tests/integration/bumble/README.md)
- License: Apache License 2.0

BSWorld uses the public Python and HCI transport APIs without modifying the Bumble Host stack.

## Zephyr Project

- Source: https://github.com/zephyrproject-rtos/zephyr
- Usage: [Virtual Controller](docs/controllers.md#using-a-zephyr-controller) and [Bluetooth and Mesh shells](tests/integration/zephyr/shell/README.md)
- License: Apache License 2.0

The Virtual Controller is Zephyr's `samples/bluetooth/hci_virt` with a BSWorld Kconfig overlay. The shell applications use external Zephyr sources.

## QEMU

- Upstream source: https://gitlab.com/qemu-project/qemu
- BSWorld fork: https://github.com/LingaoM/qemu
- Usage: [Linux Guest execution](tests/integration/qemu/README.md), with instruction-counting time synchronized to BabbleSim
- License: GNU General Public License version 2 for QEMU as a whole; individual files may carry compatible licenses

QEMU is not vendored in this repository. BSWorld uses the modified fork linked above. Redistribution of its executable remains subject to the applicable corresponding-source and notice obligations.

## Buildroot

- Source: https://gitlab.com/buildroot.org/buildroot
- Usage: builds the [Linux Guest environment](tests/integration/qemu/README.md)
- License: GNU General Public License version 2 or later, with exceptions and per-file licenses described by the upstream `COPYING` file

Only BSWorld's [external configuration](tests/integration/qemu/source/README.md) is maintained here. Buildroot's license does not replace the licenses of the assembled software. Review the package licenses, notices, and corresponding-source material in the actual build's `legal-info` output before distributing a Guest image.

## BlueZ

- Source: https://git.kernel.org/pub/scm/bluetooth/bluez.git
- Usage: Bluetooth Host stack and tools for [Linux Guest tests](tests/integration/qemu/bluez/README.md)
- License: GNU General Public License version 2 or later and GNU Lesser General Public License version 2.1 or later, depending on the component

The Guest also includes D-Bus, BusyBox, glibc, and other dependencies. Their licenses and notices are recorded in the corresponding Buildroot `legal-info` output.
