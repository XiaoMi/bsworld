# Zephyr 周期广播示例测试

[English](README.md) | [中文](README.zh-CN.md)

验证周期广播发送和同步。

## 构建

推荐使用的 [Docker 测试镜像](../../../../../docker/README.zh-CN.md#运行测试)已包含本组固件。按文档中的套件选择命令运行 `tests/integration/zephyr/samples/periodic_advertising`。以下命令用于本机 Linux 构建。

先按上一级 README 的[环境准备](../README.zh-CN.md#环境准备)准备 Zephyr、BabbleSim 和虚拟 Controller，保留工作区及编译变量。以下命令只构建本组使用的两个上游示例：

```sh
source "$ZEPHYR_WORKSPACE/.venv/bin/activate"
cd "$ZEPHYR_WORKSPACE"

west build -p always -b nrf52_bsim \
  -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_periodic_adv" \
  "$ZEPHYR_WORKSPACE/zephyr/samples/bluetooth/periodic_adv" \
  -- -DEXTRA_CONF_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim.conf" \
  -DDTC_OVERLAY_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim_host_only.overlay"
export BSWORLD_ZEPHYR_PERIODIC_ADV_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_periodic_adv/zephyr/zephyr.exe"

west build -p always -b nrf52_bsim \
  -d "$ZEPHYR_WORKSPACE/build/bsworld_zephyr_periodic_sync" \
  "$ZEPHYR_WORKSPACE/zephyr/samples/bluetooth/periodic_sync" \
  -- -DEXTRA_CONF_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim.conf" \
  -DDTC_OVERLAY_FILE="$BSWORLD_ROOT/tests/integration/zephyr/samples/common/source/bsim_host_only.overlay"
export BSWORLD_ZEPHYR_PERIODIC_SYNC_BIN="$ZEPHYR_WORKSPACE/build/bsworld_zephyr_periodic_sync/zephyr/zephyr.exe"
```

## 运行

在同一个 shell 中切回 BSWorld Python 环境，保留导出的产物变量：

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
cd "$BSWORLD_ROOT"
pytest tests/integration/zephyr/samples/periodic_advertising -q --no-ellisys
```
