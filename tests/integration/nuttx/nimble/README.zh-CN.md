# NuttX NimBLE 测试

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试运行 nuttx-apps 中的 [Apache NimBLE](https://github.com/apache/mynewt-nimble) 示例：`nimble_bleprph` 作为 Peripheral，`nimble_blecent` 作为 Central。每个进程连接自己的 BSController（默认的[虚拟 Controller](../../../../docs/zh-CH/controllers.md)），由 pytest 通过 shell 命令驱动。

| 程序 | 上游配置 | 可执行文件变量 |
|---|---|---|
| NimBLE | NuttX `sim:nimble`，启用 `nimble_bleprph` 和 `nimble_blecent` | `BSWORLD_NUTTX_NIMBLE_BIN` |

[NuttX Docker 镜像](../../../../docker/README.zh-CN.md#选择测试套件)已包含该程序并设置好变量。以下步骤用于本机 Linux 构建。

## 准备环境

先按 [NuttX 测试环境](../README.zh-CN.md#准备环境)准备 `$NUTTX_NIMBLE_DIR` 和 `$NUTTX_NIMBLE_APPS_DIR` 工作区，并构建 BabbleSim。

## 构建与运行

配置 `sim:nimble`，启用 BabbleSim 时钟和两个示例，并打开 Peripheral、Central、Observer 角色。配置时需保留 BabbleSim 构建时导出的 `BSIM_OUT_PATH` 和 `BSIM_COMPONENTS_PATH`：

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
cd "$NUTTX_NIMBLE_DIR"
# Only needed when this tree was previously configured.
if [ -f .config ]; then make distclean; fi
./tools/configure.sh -l -a "$(realpath --relative-to="$NUTTX_NIMBLE_DIR" "$NUTTX_NIMBLE_APPS_DIR")" sim:nimble
kconfig-tweak --file .config -e SIM_BSIM_TIME \
  -d NIMBLE_PORTING_EXAMPLE -d EXAMPLES_NIMBLE \
  -e EXAMPLES_NIMBLE_BLEPRPH -e EXAMPLES_NIMBLE_BLECENT \
  -e NIMBLE_ROLE_PERIPHERAL -e NIMBLE_ROLE_CENTRAL -e NIMBLE_ROLE_OBSERVER
make olddefconfig
```

编译程序：

```sh
make -j"$(nproc)"
export BSWORLD_NUTTX_NIMBLE_BIN="$NUTTX_NIMBLE_DIR/nuttx"
```

运行测试：

```sh
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/nimble -q
```

| 文件 | 场景 | Controller |
|---|---|---|
| [test_central_peripheral.py](test_central_peripheral.py) | Central 扫描、连接、服务发现和订阅通知 | 两种 Controller 各跑一次 |

## 添加场景

```python
from tests.integration.nuttx.common import create_nuttx_process
from tests.integration.nuttx.nimble.devices import NUTTX_NIMBLE_PERIPHERAL

peripheral = create_nuttx_process(
    topo, NUTTX_NIMBLE_PERIPHERAL, log_subdir="nuttx/nimble/peripheral"
)
peripheral.attach(topo.add_controller(public_address="C0:FF:EE:20:00:01"))
await topo.start()
await peripheral.shell.run_many(("ifup bnep0", "bleprph"))
await peripheral.wait_log(r"connection established; status=0 handle=\d+")
```

Profile 只描述程序名称、可执行文件和 shell 行为。测试通过 `topo.add_controller()` 选择 Controller 类型和地址，通过 `host.attach(controller)` 绑定程序；创建函数已经将程序登记到传入的拓扑中。

| 角色 | Shell 命令 |
|---|---|
| Peripheral | `ifup bnep0`、`bleprph` |
| Central | `ifup bnep0`、`blecent` |

[devices.py](devices.py) 提供 Peripheral 和 Central 的设备配置。`log_subdir` 指定进程输出目录，现有测试使用 `nuttx/nimble/<role>/`，其余见[产物目录结构](../../../../docs/zh-CH/artifacts.md)。

## 许可证

本目录的测试使用 Apache License 2.0，其余见[上级许可证说明](../README.zh-CN.md#许可证)。
