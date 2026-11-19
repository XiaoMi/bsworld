# UML BlueZ 测试

[English](README.md) | [简体中文](README.zh-CN.md)

在 [UML Guest](../README.zh-CN.md) 中运行 Ubuntu i386 软件包中的 `bluetoothd`、`bluetoothctl` 和 `btattach`。每个 Guest 启动自己的 D-Bus 和 `bluetoothd`，[main.conf](main.conf) 安装在 `/opt/bsworld/bluez.conf`。

## 构建与运行

准备 [UML 测试环境](../README.zh-CN.md#准备环境)，然后在仓库根目录选择 BlueZ 套件。

```sh
docker run --rm --security-opt seccomp=docker/uml-seccomp.json \
  bsworld-linux:dev pytest tests/integration/uml/bluez -q
```

| 测试 | 验证内容 |
|---|---|
| `test_uml_bluez_guests_advertise_to_bumble_observer` | 两个 Guest 各自注册并上电 `hci0`，同一个 Bumble Observer 收到两者的广播，名称和地址均正确。 |
| `test_uml_bluez_scans_and_connects_bumble_peripheral` | BlueZ 发现 Bumble Peripheral，连接后断开。 |

控制台日志和 HCI 抓包位于 `uml/bluez/`。广播测试为每个 Guest 创建一个子目录。[测试产物](../../../../docs/zh-CH/artifacts.md)
