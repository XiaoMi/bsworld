# UML Zephyr Host 测试

[English](README.md) | [简体中文](README.zh-CN.md)

Zephyr Bluetooth shell 在 UML Guest 中运行，通过 BSController 与 Bumble 对测。

## 构建与运行

Shell 在 Guest 内的默认路径是 `/opt/bsworld/zephyr-shell`。如需更换可执行文件，将 `BSWORLD_UML_ZEPHYR_BIN` 设为新文件在 Guest 内的绝对路径。

在 Docker 中运行时，使用[选择测试套件](../../../../docker/README.zh-CN.md#选择测试套件)中的 Zephyr 命令。

本地构建前，先准备 [UML 环境](../README.zh-CN.md#本地-linux)。按 [Dockerfile](../../../../docker/linux.Dockerfile) 的 `zephyr-uml` 阶段指定的版本和模块准备 west 工作区。

将 `ZEPHYR_WORKSPACE` 指向该工作区，并激活它的 Python 环境。然后在 BSWorld 仓库根目录使用 [prj.conf](prj.conf) 构建并安装 shell。

```sh
BSWORLD_CHECKOUT="$PWD"
(
  cd "$ZEPHYR_WORKSPACE" &&
  ZEPHYR_TOOLCHAIN_VARIANT=host west build -b native_sim/native \
    -d build/bsworld_uml_zephyr zephyr/tests/bluetooth/shell \
    -- -DCONF_FILE="$BSWORLD_CHECKOUT/tests/integration/uml/zephyr/prj.conf"
) && install -D "$ZEPHYR_WORKSPACE/build/bsworld_uml_zephyr/zephyr/zephyr.exe" \
  "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/zephyr-shell"
```

在 BSWorld 的 Python 环境中运行测试。

```sh
pytest tests/integration/uml/zephyr --require-integration -q
```

## 测试覆盖

| 测试文件 | 场景 |
|---|---|
| [test_discovery.py](test_discovery.py) | 主动和被动扫描、发现 Zephyr 广播、使用 Filter Accept List 连接，以及以 LE Coded PHY 建连后切换至 LE 2M。 |
| [test_gatt.py](test_gatt.py) | Zephyr 发送加密通知，并作为 GATT Client 发现和读取 Bumble 的属性。 |
| [test_link.py](test_link.py) | 更新 PHY、数据长度和连接参数，以及更新信道映射后继续传输 GATT 数据。 |
| [test_security.py](test_security.py) | 绑定后通过非定向和定向广播重新连接，验证 RPA 解析与恢复加密。 |
| [test_periodic_advertising.py](test_periodic_advertising.py) | 双向周期广播和同步，以及同步丢失与恢复。 |
| [test_periodic_sync_transfer.py](test_periodic_sync_transfer.py) | Zephyr 在两种 ACL 角色下发送和接收广播集信息及第三台设备的周期广播同步。 |

## 测试产物

各用例的 `uml/zephyr/` 目录包含控制台日志 `zephyr.log`、Controller 的 HCI BTSnoop 和空口 PCAPNG 抓包。输出路径和文件格式见[日志、跟踪文件与抓包](../../../../docs/zh-CH/artifacts.md)。
