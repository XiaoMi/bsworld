# bt_virt_controller

[English](README.md) | [中文](README.zh-CN.md)

`bt_virt_controller` 是 Xiaomi 为 BSWorld 开发的虚拟 Controller 实现。它的源代码独立于 Zephyr 源码树维护，并面向 [nrf52_bsim](https://docs.zephyrproject.org/latest/boards/native/nrf_bsim/doc/nrf52_bsim.html) 构建。该程序运行完整的 HCI 接口和 Link Layer 协议逻辑，并通过 BabbleSim PHY 参与无线数据收发和 RF 交互。虚拟 Controller 提供基于 Unix socket 的双向 H4 传输通道，以及可选的非阻塞 BTSnoop 抓包 socket。

本目录保留 Application 源码、配置和构建说明。编译产物位于 Zephyr 工作区，通过 `BSWORLD_BT_VIRT_CONTROLLER_BIN` 指定。

## 运行接口

BSWorld 为每个 HCI socket 启动一个虚拟 Controller：

```text
"$BSWORLD_BT_VIRT_CONTROLLER_BIN" \
  -s=<sim_id> \
  -d=<device_id> \
  [-hci-poll-interval=<microseconds>] \
  -hci-socket=<hci_socket_path> \
  [-bd-addr=<AA:BB:CC:DD:EE:FF>] \
  [-btsnoop-socket=<btsnoop_socket_path>]
```

- `-s` 和 `-d` 分别指定 BabbleSim 仿真标识和 Controller 设备编号。
- `-hci-poll-interval` 设置 HCI socket 轮询周期，并将同一个值应用为 PHY 最大重同步间隔；默认值为 1,000 微秒（1 ms）。
- `-hci-socket` 承载双向 H4 HCI 帧。
- `-bd-addr` 用于在 Host 连接前初始化 Controller 的 public address。
- `-btsnoop-socket` 开启独立抓包通道；即使抓包端处理不及时，也不会阻塞 HCI 数据路径。

## 构建

推荐使用的 [Docker 测试镜像](../../docker/README.zh-CN.md#使用已发布镜像)已包含 Controller。以下命令用于本机 Linux 构建。

先按照[README 构建说明](../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim，并保留 `BSWORLD_ROOT`、`BSIM_OUT_PATH`、`BSIM_COMPONENTS_PATH` 和 `BSIM_BASE_PATH`。本机请先按 [Zephyr 环境说明](../../tests/integration/zephyr/samples/common/source/README.zh-CN.md)设置 `ZEPHYR_WORKSPACE`。

直接使用官方 [west build](https://docs.zephyrproject.org/latest/develop/west/build-flash-debug.html) 命令：

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"
west build -p always -b nrf52_bsim \
  -d "$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller" \
  "$BSWORLD_ROOT/components/bt_virt_controller"
export BSWORLD_BT_VIRT_CONTROLLER_BIN="$ZEPHYR_WORKSPACE/build/bsworld_bt_virt_controller/zephyr/zephyr.exe"
"$BSWORLD_BT_VIRT_CONTROLLER_BIN" --help
```

运行 pytest 前，在同一个 shell 中切回 BSWorld Python 环境：

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
```

默认使用开源 Zephyr Bluetooth Controller。使用 nRF Connect SDK 构建的等价程序应按其自身许可处理。

## 许可证与依赖

本目录中的 BSWorld 源码归 Xiaomi Corporation 所有，使用 Apache License 2.0。Zephyr 和 BabbleSim 遵循各自的许可证，详见[第三方声明](../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
