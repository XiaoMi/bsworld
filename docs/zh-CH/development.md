# 开发和单元测试

[English](../development.md) | [中文](development.md)

推荐使用[已发布的 Docker 镜像](../../docker/README.zh-CN.md#使用已发布镜像)执行开发检查和集成测试。将仓库挂载到 `/workspace` 即可检查当前代码，命令见 [Docker 测试说明](../../docker/README.zh-CN.md#运行测试)。需要本机 Python 环境时，按 README 的[本地 Linux 环境搭建](README.md#本地-linux)配置。

## 运行单元测试

使用已发布镜像，在仓库根目录运行：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest pytest tests/unit -q -p no:cacheprovider
```

使用本机 Python 环境时：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/unit -q
```

## 单元测试覆盖与组织

`tests/unit/` 按子系统组织测试，每个测试聚焦一项稳定的对外行为，名称应描述所验证的行为。

| 测试文件 | 覆盖内容 |
|---|---|
| `test_plugin.py` | pytest 插件参数、输出目录、xdist 运行标识共享和 `--no-hci-dump` |
| `test_env.py` | 环境变量读取、产物路径解析和运行环境识别 |
| `test_artifact_paths.py` | Zephyr、NuttX 和 QEMU 测试使用环境变量指定的产物路径 |
| `test_dependency_readiness.py` | BabbleSim 程序与动态模型、Controller 文件和执行权限检查 |
| `test_topology.py` | 测试拓扑生命周期、业务程序登记、通用进程管理和 Controller 分配 |
| `test_babblesim_backend.py` | BabbleSim 进程参数、设备编号与 Controller 初始化、RF 干扰与路径衰减、跟踪文件转换和清理 |
| `test_hci.py` | 虚拟 Controller BTSnoop socket 抓包 |
| `test_scheduler.py` | 离散仿真时间、标准 asyncio 原语、回调和 I/O 执行顺序 |
| `test_bsim_clock.py` | FIFO 注册、二进制消息编码、连接与应答超时、不完整读写和断连处理 |
| `test_runtime.py` | FIFO 日志采集、完整行匹配和 `wait_log` |
| `test_interference.py` | RF 干扰与路径衰减配置校验 |

业务程序行为、RF 传播本身，以及端到端 Bluetooth LE 示例，由 `tests/integration/*` 下的仿真测试覆盖。

## 静态检查

使用 [Ruff](https://docs.astral.sh/ruff/) 执行 Python 静态检查：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest ruff check --no-cache src tests
```

使用本机 Python 环境时，可执行检查或自动修复：

```sh
ruff check src tests
ruff check --fix src tests
```

配置在 `pyproject.toml` 的 `[tool.ruff]` 下。

## 文档

项目文档使用普通 Markdown 编写。英文公共文档放在 `docs/` 下，对应的中文版本统一放在 `docs/zh-CH/` 下。`components/` 和 `tests/` 中面向具体组件的 README 仍与组件放在一起，中文文件使用 `.zh-CN.md` 后缀。

仓库根目录的 README 是文档入口。功能行为变化时，英文和中文页面需要同步更新。

## 扩展 BSWorld

接入大多数新的 Bluetooth LE 业务程序环境时，只需要在对应测试目录中增加辅助函数和测试用例；涉及原生二进制时在 README 中记录官方构建命令和环境变量。Zephyr 和 NuttX 把进程启动约定放在各自的集成测试目录里，因为不同环境使用的启动参数格式并不相同。Bumble 这类 Python 协议栈可以直接运行在 pytest 中，并连接 Controller socket；BlueZ 这类 Linux 协议栈可以运行在 QEMU Guest 中，由 HCI UART 桥接到相同的 socket 接口。每个登记到拓扑中的业务程序都会分配一个独立的虚拟 Controller。

只有新的接入方式改变了框架的执行约定时，才需要修改公共代码：

- **新的进程启动格式**：增加或扩展测试侧适配代码，由它创建带有该平台 BabbleSim 设备标识参数和 HCI socket 参数的 `ProcessInstance`，并在所属集成测试中实际运行这套适配代码。
- **新的 RF 模型封装**：在 `src/bsworld/interference.py` 中增加对外配置类型和参数校验，在 `src/bsworld/backends/babblesim.py` 中转换为 BabbleSim 参数，并分别测试非法输入和最终生成的命令。

平台专用的进程配置、源码配置和构建说明应留在对应测试目录。只有被多个业务程序环境共同使用，或者属于 BSWorld 公共运行模型的能力，才放入框架源码。

## 测试覆盖率

分支覆盖率由 [coverage.py](https://coverage.readthedocs.io/) 统计，并在 `pyproject.toml` 的 `[tool.coverage.run]` 中启用。

以下命令在本机 Python 环境中执行：

```sh
# 只统计单元测试覆盖率，速度快，不需要固件或 BabbleSim
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m coverage run -m pytest \
  -p pytest_asyncio.plugin -p bsworld.plugin tests/unit
python -m coverage report

# Native integration tests after compiling the required firmware
pytest tests/integration/ -n auto
```

这组命令会先启动覆盖率记录，再由 pytest 导入项目代码；同时关闭无关的第三方插件自动加载，只显式启用测试所需的 asyncio 和 BSWorld 插件。最终报告会统计整个 `bsworld` 包，包括 runtime 调度器。
