# UML BlueZ 测试

[English](README.md) | [简体中文](README.zh-CN.md)

测试通过 `bluetoothctl` 操作 Ubuntu 24.04 的 i386 BlueZ。每个 UML Guest 独立运行 D-Bus 和 `bluetoothd`。

## 运行测试

按[本地 Linux 指南](../README.zh-CN.md#本地-linux)组装根文件系统后，在仓库根目录安装 `bluetoothd` 启动时读取的测试配置。

```sh
install -Dm644 tests/integration/uml/bluez/main.conf \
  "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/bluez.conf"
```

运行 BlueZ 套件。

```sh
pytest tests/integration/uml/bluez --require-integration -q
```

在 Docker 中运行时，通过挂载输出目录保留日志和抓包。命令见[选择测试套件](../../../../docker/README.zh-CN.md#选择测试套件)。

## 测试覆盖

| 测试 | 验证内容 |
|---|---|
| `test_uml_bluez_guests_advertise_to_bumble_observer` | 两个 Guest 各自注册并启用 `hci0`。同一个 Bumble Observer 收到两者的广播，名称和地址均符合预期。 |
| `test_uml_bluez_scans_and_connects_bumble_peripheral` | BlueZ 发现 Bumble Peripheral，连接后断开。 |

## 测试产物

各用例将控制台日志、HCI BTSnoop 和 Controller 的空口 PCAPNG 抓包写入 `uml/bluez/`。广播用例为每个 Guest 再建一个子目录。完整目录结构见[测试产物说明](../../../../docs/zh-CH/artifacts.md)。
