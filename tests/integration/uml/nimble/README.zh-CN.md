# UML NimBLE Linux 测试

[English](README.md) | [简体中文](README.zh-CN.md)

在 [UML Linux Guest](../README.zh-CN.md) 中运行 [Apache NimBLE](https://github.com/apache/mynewt-nimble) 上游的 Linux Host 示例，Bumble 作为对端。

| 程序 | 上游版本 | 可执行文件变量 |
|---|---|---|
| `nimble-linux` | `porting/examples/linux`，提交 `1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845` | `BSWORLD_UML_NIMBLE_BIN` |

## 准备环境

先按 [UML 测试环境](../README.zh-CN.md#编译-linux-guest)编译内核和随机种子库。安装 mbedtls 头文件（Ubuntu）：

```sh
sudo apt-get install -y libmbedtls-dev
```

在 Linux 源码树旁获取 Apache NimBLE：

```sh
export NIMBLE_DIR="$UML_DIR/mynewt-nimble"
git init "$NIMBLE_DIR"
git -C "$NIMBLE_DIR" fetch --depth 1 https://github.com/apache/mynewt-nimble.git 1e8ed60276f35a80ed4d4b4f8bb9d9c6fee53845
git -C "$NIMBLE_DIR" checkout --detach FETCH_HEAD
```

## 构建与运行

Guest 直接从主机文件系统运行该示例。上游默认编译 32 位版本（`-m32`），因此为 64 位 Guest 清空这两个参数：

```sh
make -C "$NIMBLE_DIR/porting/examples/linux" NIMBLE_CFLAGS= NIMBLE_LDFLAGS= -j"$(nproc)"
export BSWORLD_UML_NIMBLE_BIN="$NIMBLE_DIR/porting/examples/linux/nimble-linux"
```

运行测试：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/uml/nimble -q
```

`test_uml_nimble_linux_advertises_and_accepts_connection`：NimBLE 以 `AA:BB:CC:DD:EE:FE` 和设备名 `nimble` 广播，Bumble Central 连接后断开。

NimBLE 停止 `bluetoothd` 后通过 HCI User Channel 独占 `hci0`。Guest 控制台位于 `uml/nimble/uml_nimble.log`，其余见[产物目录结构](../../../../docs/zh-CH/artifacts.md)。

## 许可证

本目录的测试使用 Apache License 2.0，Apache NimBLE 同样使用 Apache License 2.0，其余见[上级许可证说明](../README.zh-CN.md#许可证)。
