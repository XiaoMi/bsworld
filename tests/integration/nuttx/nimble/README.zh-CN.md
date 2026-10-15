# NuttX NimBLE 测试

[English](README.md) | [中文](README.zh-CN.md)

这个目录保存 BSWorld 对 [Apache NimBLE](https://github.com/apache/mynewt-nimble) Host 的 NuttX 集成测试。测试运行 nuttx-apps 中的 NimBLE 示例。

当前测试会启动：

- `examples/nimble_bleprph`：作为 Peripheral。
- `examples/nimble_blecent`：作为 Central。

Peripheral 会广播原生示例默认的 `NuttX NimBLE PRPH` 名称和 Alert Notification Service UUID。Central 负责扫描、发现广播、建立连接、执行服务发现并订阅通知。测试检查这些 NuttX 示例输出的日志。

两个角色使用同一份 NuttX 镜像，进程配置默认启用 Shell。`await topo.start()` 建立初始 PHY 同步边界后，测试通过 `process.shell.run_many(...)` 发送 `ifup bnep0` 和对应的 `bleprph` 或 `blecent` 命令。

固件产物的构建方式见 [`source/README.zh-CN.md`](source/README.zh-CN.md)。首次准备 NuttX 环境时，先看 [../README.zh-CN.md](../README.zh-CN.md)。

## 文件职责

- `devices.py`：定义 Central 和 Peripheral 使用的 `NuttXProcessProfile`。两个角色使用同一份 NuttX 镜像。
- `test_central_peripheral.py`：端到端测试，验证 Central 能发现 Peripheral、建立连接并完成 NimBLE 示例默认的服务发现流程。
- `source/`：NuttX 上游 NimBLE 镜像的构建配置和说明。

## Shell 命令

Peripheral 进程启动后执行：

```sh
ifup bnep0
bleprph
```

Central 进程启动后执行：

```sh
ifup bnep0
blecent
```

测试会等待 NuttX 示例自身输出的连接、服务发现和订阅日志。

## 运行

推荐使用 [Docker 测试镜像](../../../../docker/README.zh-CN.md#选择测试套件)，将 pytest 测试路径设为 `tests/integration/nuttx/nimble`。

本机 Linux 按 [source/README.zh-CN.md](source/README.zh-CN.md) 构建，保留 BabbleSim 和 Controller 变量，再切回 BSWorld Python 环境并运行：

```sh
source "$BSWORLD_ROOT/.venv/bin/activate"
export BSWORLD_NUTTX_NIMBLE_BIN="$NUTTX_NIMBLE_DIR/nuttx"
cd "$BSWORLD_ROOT"
pytest tests/integration/nuttx/nimble -q --no-ellisys
```
