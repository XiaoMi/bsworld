# UML BlueZ 测试

[English](README.md) | [简体中文](README.zh-CN.md)

在 [UML Linux Guest](../README.zh-CN.md) 中运行主机的 BlueZ，通过 `bluetoothctl` 控制，Bumble 作为对端。

| 程序 | 来源 | 可执行文件变量 |
|---|---|---|
| `bluetoothd`、`bluetoothctl`、`btattach` | 主机已安装的 BlueZ，已用 `bluetoothd` 5.86 和 `bluetoothctl` 5.64 验证 | 无 |

## 准备环境

先按 [UML 测试环境](../README.zh-CN.md#编译-linux-guest)编译内核和随机种子库。主机需要安装 64 位的 BlueZ；[init.sh](../source/init.sh) 启动 `/usr/libexec/bluetooth/bluetoothd`，不存在时启动 `/usr/lib/bluetooth/bluetoothd`。

## 构建与运行

这些测试不需要编译 BlueZ。运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/uml/bluez -q
```

| 测试 | 验证内容 |
|---|---|
| `test_uml_bluez_initializes_virtual_controller` | `hci0` 以预期的公共地址注册并上电。 |
| `test_uml_bluez_advertises_to_bumble_observer` | Bumble Observer 收到 BlueZ 的可连接广播，设备名和地址正确。 |
| `test_uml_bluez_scans_and_connects_bumble_peripheral` | BlueZ 发现 Bumble Peripheral，建立连接后断开。 |

BlueZ 通过 `bluetoothd` 使用 `hci0`。Guest 控制台位于 `uml/bluez/uml_bluez.log`，其余见[产物目录结构](../../../../docs/zh-CH/artifacts.md)。

## 许可证

本目录的测试使用 Apache License 2.0，其余见[上级许可证说明](../README.zh-CN.md#许可证)。
