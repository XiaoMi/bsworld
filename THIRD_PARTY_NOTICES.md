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
- Usage: [NuttX test applications](tests/integration/uml/README.md)
- License: Apache License 2.0

NuttX source is obtained externally and is not vendored in this repository.

## Apache NimBLE

- Source: https://github.com/apache/mynewt-nimble
- Usage: Bluetooth Host stack for [NuttX](tests/integration/uml/README.md) and [Linux Guest tests](tests/integration/uml/nimble/README.md)
- License: Apache License 2.0

NimBLE source is obtained externally and is not vendored in this repository.

## Bumble

- Source: https://github.com/google/bumble
- Usage: Python Bluetooth Host stack for [Bumble test peers](tests/integration/uml/bumble_peer.py) and [BSController samples](components/bscontroller/samples/README.md)
- License: Apache License 2.0

BSWorld uses the public Python and HCI transport APIs without modifying the Bumble Host stack.

## Zephyr Project

- Source: https://github.com/zephyrproject-rtos/zephyr
- Usage: [Zephyr Bluetooth shell tests](tests/integration/uml/README.md)
- License: Apache License 2.0

BSWorld builds the Bluetooth shell from external Zephyr sources using its [native_sim configuration](tests/integration/uml/zephyr/prj.conf).

## Linux

- Source: https://github.com/torvalds/linux
- Usage: [Linux Guest execution](tests/integration/uml/README.md) as User Mode Linux, with time-travel synchronized to BabbleSim
- License: GNU General Public License version 2; individual files may carry compatible licenses, as described by the upstream `COPYING` file

The Guest kernel is built from an unmodified upstream commit with BSWorld's [configuration fragment](src/bsworld/uml/linux.fragment).

## Moby seccomp profile

The [UML container profile](docker/uml-seccomp.json) uses Moby `seccomp/v0.2.4` with permission added for `personality(ADDR_NO_RANDOMIZE)`. [Upstream profile](https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/seccomp/default.json)

## BlueZ

- Source: https://git.kernel.org/pub/scm/bluetooth/bluez.git
- Usage: Bluetooth Host stack and tools for [Linux Guest tests](tests/integration/uml/bluez/README.md)
- License: GNU General Public License version 2 or later and GNU Lesser General Public License version 2.1 or later, depending on the component

The UML Guest root filesystem is assembled from Ubuntu 24.04 i386 packages. Their licenses are available in the Guest at `/usr/share/doc/<package>/copyright`.
