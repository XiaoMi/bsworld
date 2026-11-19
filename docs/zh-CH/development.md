# 开发检查

[English](../development.md) | [简体中文](development.md)

准备好[本地 Linux 环境](README.md#本地-linux)后，从仓库根目录运行开发检查。Docker 镜像的选择方法见[验证修改](#验证修改)。

## 单元测试

激活本地虚拟环境并运行单元测试。

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/unit -q
```

在 Docker 中验证检出的代码时，将仓库挂载到测试镜像后运行 pytest。

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  ghcr.io/xiaomi/bsworld-linux:latest pytest tests/unit -q -p no:cacheprovider
```

单元测试验证 `src/bsworld/` 中的框架代码，覆盖拓扑和 Controller 生命周期、RF 配置、仿真时间调度、FIFO 时钟通信、UML Guest 生命周期和 time-travel，以及日志收集。

## 代码格式

两个 Python 包都固定使用 Ruff 0.14.10。Ruff 检查本仓库维护的 Python 源码、示例和测试，统一排序 import，按 88 字符的目标行宽和四空格缩进格式化代码，并保留原有的引号风格。

`E501` 检查长行，包括注释和字符串。Ruff 允许无法拆分的单词和行尾 URL。两个公开的蓝牙错误标识符使用行内 `# noqa: E501` 保留现有 API 名称。说明性文字请手动换行，豁免应限定在具体行。

在仓库根目录的 Python 开发环境中运行 Ruff 检查。

```sh
ruff check .
ruff format --check .
```

在本地自动排序 import 并整理格式。

```sh
ruff check --select I --fix .
ruff format .
```

发起针对 `main` 的 Pull Request 或向 `main` 推送时，CI 会在 `Checks` 中运行两项 Ruff 检查和单元测试。

## 测试覆盖率

`pyproject.toml` 为 `bsworld` 包配置了分支覆盖率统计，范围包括运行时调度器。在本地环境中运行单元测试并生成报告。

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m coverage run -m pytest \
  -p pytest_asyncio.plugin -p bsworld.plugin tests/unit
python -m coverage report
```

## 验证修改

在本地验证端到端蓝牙行为时，构建所需固件后运行集成测试。

```sh
pytest tests/integration/ -n auto
```

将检出的代码挂载到 `/workspace` 后，容器使用镜像中的可执行程序运行测试。在 Docker 中运行 UML 集成测试时，传入 [UML seccomp 配置](../../docker/uml-seccomp.json)。

| 修改内容 | 验证方式 |
|---|---|
| Python 框架、测试或场景 | 使用同一提交的镜像挂载代码，见[运行测试](../../docker/README.zh-CN.md#运行测试)。 |
| 固件、固件构建输入或 Dockerfile | 从当前代码[构建镜像](../../docker/README.zh-CN.md#构建)，或使用 Pull Request 的 CI 构建。 |

本地结果与 CI 不一致时，先用同一提交的镜像 `sha-<commit>` 复现，再对比编译器、Python 及 UML Guest 根文件系统中的 i386 BlueZ 和 D-Bus 包版本。
