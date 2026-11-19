# UML NuttX 测试

[English](README.md) | [简体中文](README.zh-CN.md)

BTSak 和 NimBLE 均使用 NuttX 的 `sim` 程序，在 UML Guest 中与 Bumble 对测。

## 准备环境

两个套件共用 [UML 测试环境](../README.zh-CN.md#准备环境)。镜像的 `nuttx-uml` 阶段用固定版本的 NuttX 和 NuttX Apps 编译两个 i386 程序。源码版本和构建依赖见 [Dockerfile](../../../../docker/linux.Dockerfile)。

| 套件 | NuttX 配置 | Guest 内默认可执行文件 | 覆盖变量 |
|---|---|---|---|
| BTSak | `sim:bthcisock` | `/opt/bsworld/nuttx-btsak` | `BSWORLD_UML_NUTTX_BIN` |
| NimBLE | `sim:nimble` | `/opt/bsworld/nuttx-nimble` | `BSWORLD_UML_NUTTX_NIMBLE_BIN` |

使用其他可执行文件时，将对应变量设为 Guest 内的绝对路径。

在本地构建时，为 BTSak 和 NimBLE 分别准备 NuttX 和 NuttX Apps 源码。源码目录和 `BSWORLD_UML_ROOTFS` 均填写主机上的绝对路径，后者指向可写的 Guest 根目录。

从 BSWorld 仓库根目录运行两个套件。

```sh
pytest tests/integration/uml/nuttx --require-integration -q
```

在 Docker 中运行时，通过挂载输出目录保留日志和抓包。命令见[选择测试套件](../../../../docker/README.zh-CN.md#选择测试套件)。

## BTSak

使用已准备的 `NUTTX_DIR` 和 `NUTTX_APPS_DIR` 编译并安装。

```sh
(
  cd "$NUTTX_DIR" &&
  ./tools/configure.sh -l -a "$(realpath --relative-to=. "$NUTTX_APPS_DIR")" sim:bthcisock &&
  kconfig-tweak --file .config -e SIM_M32 -d BLUETOOTH_SMP_SELFTEST &&
  make olddefconfig &&
  make -j"$(nproc)" &&
  install -Dm755 nuttx "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/nuttx-btsak"
) && pytest tests/integration/uml/nuttx/test_btsak.py --require-integration -q
```

[BTSak 用例](test_btsak.py)验证 Bumble 能收到 NuttX 的可连接广播，NuttX 能扫描到一个或两个 Bumble 广播端。

定向干扰期间，一台 NuttX 扫描端仍能收到广播。受到干扰的扫描端在干扰结束后才收到广播。

## NimBLE

使用已准备的 `NUTTX_NIMBLE_DIR` 和 `NUTTX_NIMBLE_APPS_DIR` 编译并安装。

```sh
(
  cd "$NUTTX_NIMBLE_DIR" &&
  ./tools/configure.sh -l -a "$(realpath --relative-to=. "$NUTTX_NIMBLE_APPS_DIR")" sim:nimble &&
  kconfig-tweak --file .config -e SIM_M32 \
    -d NIMBLE_PORTING_EXAMPLE -d EXAMPLES_NIMBLE \
    -e EXAMPLES_NIMBLE_BLEPRPH -e EXAMPLES_NIMBLE_BLECENT \
    -e NIMBLE_ROLE_PERIPHERAL -e NIMBLE_ROLE_CENTRAL -e NIMBLE_ROLE_OBSERVER &&
  make olddefconfig &&
  make -j"$(nproc)" &&
  install -Dm755 nuttx "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/nuttx-nimble"
) && pytest tests/integration/uml/nuttx/test_nimble.py --require-integration -q
```

[NimBLE 用例](test_nimble.py)中，NuttX 运行 `bleprph` 作为 Peripheral。Bumble 连接后读取电池电量特征，并接收通知。

NuttX 以 Central 角色运行 `blecent` 时，会连接 Bumble、发现服务、读写 Alert Notification 服务中的特征，并接收电池电量通知。

## 测试产物

单 Guest 用例在 `uml/nuttx/` 下保存控制台日志、Controller 的 HCI BTSnoop 和空口 PCAPNG 抓包。干扰用例的两个扫描端分别写入 `uml/nuttx-protected/` 和 `uml/nuttx-jammed/`。

Bumble 的抓包和共享空口跟踪文件的位置见[测试产物目录](../../../../docs/zh-CH/artifacts.md#目录结构)。
