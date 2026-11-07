# NuttX BTSak 测试

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试运行 NuttX 自带的 `btsak`。每个进程连接自己的 BSController（默认的[虚拟 Controller](../../../../docs/zh-CH/controllers.md)），由 pytest 通过 shell 命令驱动，充当广播者或扫描者。

| 程序 | 上游配置 | 可执行文件变量 |
|---|---|---|
| BTSak | NuttX `sim:bthcisock` | `BSWORLD_NUTTX_BTSAK_BIN` |

## 准备环境

先按 [NuttX 测试环境](../README.zh-CN.md#准备环境)准备 `$NUTTX_DIR` 和 `$NUTTX_APPS_DIR` 工作区，并构建 BabbleSim。

## 构建与运行

配置 `sim:bthcisock`，启用 BabbleSim 时钟并关闭 SMP 自检。配置时需保留 BabbleSim 构建时导出的 `BSIM_OUT_PATH` 和 `BSIM_COMPONENTS_PATH`：

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
cd "$NUTTX_DIR"
# Only needed when this tree was previously configured.
if [ -f .config ]; then make distclean; fi
./tools/configure.sh -l -a "$(realpath --relative-to="$NUTTX_DIR" "$NUTTX_APPS_DIR")" sim:bthcisock
kconfig-tweak --file .config -e SIM_BSIM_TIME -d BLUETOOTH_SMP_SELFTEST
make olddefconfig
```

编译程序：

```sh
make -j"$(nproc)"
export BSWORLD_NUTTX_BTSAK_BIN="$NUTTX_DIR/nuttx"
```

运行测试：

```sh
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/btsak -q
```

| 文件 | 场景 |
|---|---|
| [test_btsak_scan_advertise.py](test_btsak_scan_advertise.py) | 单个与多个广播者被发现、分阶段下发 shell 命令、定向 CW 干扰与恢复 |

## 添加场景

```python
from tests.integration.nuttx.btsak.devices import NUTTX_BTSAK_ADVERTISER
from tests.integration.nuttx.common import create_nuttx_process

advertiser = create_nuttx_process(
    topo, NUTTX_BTSAK_ADVERTISER, log_subdir="nuttx/advertisers/advertiser"
)
advertiser.attach(topo.add_controller(public_address="C0:FF:EE:00:00:01"))
await topo.start()
await advertiser.shell.run_many(("ifup bnep0", "bt bnep0 advertise start"))
```

| 角色 | Shell 命令 |
|---|---|
| 广播者 | `ifup bnep0`、`bt bnep0 advertise start` |
| 扫描者 | `ifup bnep0`、`bt bnep0 scan start`，然后重复 `sleep 1`、`bt bnep0 scan get` |

[devices.py](devices.py) 提供广播者和扫描者的设备配置。`log_subdir` 指定进程输出目录，现有测试使用 `nuttx/advertisers/<name>/` 和 `nuttx/scanners/<name>/`，其余见[产物目录结构](../../../../docs/zh-CH/artifacts.md)。

## 许可证

本目录的测试使用 Apache License 2.0，其余见[上级许可证说明](../README.zh-CN.md#许可证)。
