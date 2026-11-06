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
- Usage: Bluetooth Host stack for [NuttX](tests/integration/nuttx/README.md) and [Linux Guest tests](tests/integration/uml/nimble/README.md)
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
- Usage: [Virtual Controller](docs/controllers.md#using-a-zephyr-controller) and [Bluetooth shells](tests/integration/zephyr/shell/README.md)
- License: Apache License 2.0

The Virtual Controller is Zephyr's `samples/bluetooth/hci_virt` with a BSWorld Kconfig overlay. The complete and Host-only shells use external Zephyr sources.

## Linux

- Source: https://github.com/torvalds/linux
- Usage: [Linux Guest execution](tests/integration/uml/README.md) as User Mode Linux, with time-travel synchronized to BabbleSim
- License: GNU General Public License version 2; individual files may carry compatible licenses, as described by the upstream `COPYING` file

Linux is not vendored in this repository. The Guest kernel is built from an unmodified upstream commit with BSWorld's [configuration fragment](tests/integration/uml/source/linux.fragment).

## BlueZ

- Source: https://git.kernel.org/pub/scm/bluetooth/bluez.git
- Usage: Bluetooth Host stack and tools for [Linux Guest tests](tests/integration/uml/bluez/README.md)
- License: GNU General Public License version 2 or later and GNU Lesser General Public License version 2.1 or later, depending on the component

The Guest runs BlueZ, D-Bus, and other programs installed on the host. BSWorld does not distribute them; they keep the licenses of the host's packages.
