# BSWorld NuttX BTSak 测试

[English](README.md) | [中文](README.zh-CN.md)

这个目录保存基于 NuttX 自带 `btsak` 业务程序的端到端测试。测试运行同一个包含 `btsak` 的 NuttX 镜像，再通过进程 Shell 命令让不同进程分别充当扫描设备或广播设备。

业务程序构建命令和产物路径见 [source/README.zh-CN.md](source/README.zh-CN.md)。首次准备 NuttX 环境时，先看 [../README.zh-CN.md](../README.zh-CN.md)。

## 文件职责

- `devices.py`：定义扫描设备和广播设备使用的 `NuttXProcessProfile`。两个角色使用同一份 NuttX 镜像。
- `test_btsak_scan_advertise.py`：端到端测试，验证一个扫描设备能够发现一个或多个广播设备。
- `source/`：NuttX 上游镜像的构建配置和说明。

`devices.py` 中的 `NuttXProcessProfile` 配置了 `controller_address`。BSWorld 会在 Host 连接前把该地址传给虚拟 Controller。

## Shell 命令

NuttX 进程配置默认启用 Shell。`await topo.start()` 建立初始 PHY 同步边界后，测试通过进程 Shell 下发角色命令。广播进程执行：

```sh
ifup bnep0
bt bnep0 advertise start
```

扫描进程启动后执行：

```sh
ifup bnep0
bt bnep0 scan start
sleep 1
bt bnep0 scan get
...
```

测试会等待 `bt bnep0 scan get` 输出非零设备地址。NuttX 自带的 `btsak` 广播程序当前使用固定的扫描响应名称，因此这里验证的是扫描结果表中的设备发现记录，而不是自定义广播名。角色命令写在测试用例中，可以通过 `shell.run_many(...)` 一次发送，也可以通过 `shell.run(...)` 分阶段发送。

## 测试用例

- `test_nuttx_btsak_scanner_finds_advertiser`：一个扫描设备发现一个广播设备。
- `test_nuttx_btsak_shell_commands_can_stage_scan`：先启动两个进程的 Shell，再由测试分阶段下发角色命令。
- `test_nuttx_btsak_scanner_finds_multiple_advertisers`：一个扫描设备发现两个广播设备。
- `test_nuttx_btsak_targeted_interference_isolates_and_recovers`：三个定时 CW 干扰源分别覆盖广播信道 37、38 和 39。路径衰减让干扰源靠近其中一个扫描设备，同时与另一个扫描设备隔离；未受干扰的扫描设备在干扰期间仍能接收，受干扰设备则在窗口结束后恢复。

干扰用例与其他用例使用同一份未经修改的 NuttX 镜像，变化的只有 BSWorld 定义的 RF 拓扑。

## 运行

推荐使用 [Docker 测试镜像](../../../../docker/README.zh-CN.md#选择测试套件)，将 pytest 测试路径设为 `tests/integration/nuttx/btsak`。

本机 Linux 按 [source/README.zh-CN.md](source/README.zh-CN.md) 构建，保留 BabbleSim 和 Controller 变量，再切回 BSWorld Python 环境并运行：

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
export BSWORLD_NUTTX_BTSAK_BIN="$NUTTX_DIR/nuttx"
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/btsak -q --no-ellisys
```
