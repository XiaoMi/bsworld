# UML NimBLE Linux 测试

[English](README.md) | [简体中文](README.zh-CN.md)

测试在 UML Guest 中运行 Apache NimBLE 的 Linux Host 示例，由 Bumble 充当 Central。

## 构建与运行

测试以 HCI 设备编号 `0` 启动 `/opt/bsworld/nimble-linux`。如果要更换程序，把 `BSWORLD_UML_NIMBLE_BIN` 设为新程序在 Guest 内的绝对路径。

本地编译前先准备 [UML 测试环境](../README.zh-CN.md#本地-linux)，并安装 `libmbedtls-dev:i386`。将 `NIMBLE_DIR` 指向 `NIMBLE_REVISION` 对应的源码目录，版本见 [Dockerfile](../../../../docker/linux.Dockerfile) 的 `nimble-linux` 阶段。

```sh
make -C "${NIMBLE_DIR:?}/porting/examples/linux" CC=gcc CXX=g++ -j"$(nproc)" &&
install -Dm755 "$NIMBLE_DIR/porting/examples/linux/nimble-linux" \
  "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/nimble-linux"
```

在仓库根目录运行测试。

```sh
pytest tests/integration/uml/nimble --require-integration -q
```

在 Docker 中运行时，通过挂载输出目录保留日志和抓包。命令见[选择测试套件](../../../../docker/README.zh-CN.md#选择测试套件)。

## 测试覆盖

[test_uml_nimble_linux_advertises_and_accepts_connection](test_uml_nimble_linux.py) 验证 Bumble Central 能发现地址为 `AA:BB:CC:DD:EE:FE`、名为 `nimble` 的广播端，建立连接并主动断开。

## 测试产物

用例将控制台日志 `uml_nimble.log`、Controller 的 HCI BTSnoop 和空口 PCAPNG 抓包写入 `uml/nimble/`。完整目录结构见[测试产物说明](../../../../docs/zh-CH/artifacts.md)。
