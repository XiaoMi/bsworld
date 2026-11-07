# 开发检查

[English](../development.md) | [简体中文](development.md)

请按 README 的[本地 Linux 环境搭建](README.md#本地-linux)准备开发环境，并在仓库根目录运行 BSWorld 的单元测试、静态检查和覆盖率统计。[已发布的 Docker 镜像](../../docker/README.zh-CN.md#运行测试)无需本地环境即可对 checkout 运行相同的检查，它能验证哪些修改见[验证修改](#验证修改)。

## 单元测试

使用本地虚拟环境：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/unit tests/integration/support -q
```

使用 Docker：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  ghcr.io/xiaomi/bsworld-zephyr:latest pytest tests/unit -q -p no:cacheprovider
```

单元测试覆盖环境与产物解析、拓扑和 Controller 生命周期、BabbleSim 命令生成、RF 配置、仿真时间调度、FIFO 时钟通信和日志采集。端到端 Bluetooth 行为由 `tests/integration/` 覆盖。

## 代码格式

规则覆盖本仓库维护的 Python 源码、示例和测试；依赖检出目录与生成的构建产物不参与检查。

| 语言 | 格式工具 | 规则 |
|---|---|---|
| Python | Ruff 0.14.10 | 88 个字符的目标行宽、四空格缩进、导入排序、保留已有引号风格 |

Python 由 Ruff 统一完成导入排序和格式化。两个 Python 包固定使用相同的 Ruff 版本。`E501` 继续检查格式工具无法自动折行的注释、字符串等内容。两个公开的蓝牙错误标识符为了保留现有 API 名称，使用局部 `# noqa: E501` 豁免；Ruff 也允许无法拆分的单词和行尾 URL。说明性文字应主动换行，避免增加全局豁免。

在仓库根目录，使用 Python 开发环境检查全部 Python 代码：

```sh
ruff check .
ruff format --check .
```

在本地自动整理格式：

```sh
ruff check --select I --fix .
ruff format .
```

GitHub Actions 的 `Checks` 工作流由 `CI` 在每个 pull request 和推送到 main 时运行，执行上述检查和单元测试，格式、静态规则或单元测试不通过时失败。检查覆盖检出版本中的全部 Python 代码，包括已有文件。

## 测试覆盖率

分支覆盖率已在 `pyproject.toml` 中启用。在本地环境中统计单元测试覆盖率：

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m coverage run -m pytest \
  -p pytest_asyncio.plugin -p bsworld.plugin tests/unit
python -m coverage report
```

构建所需固件后，单独运行集成测试：

```sh
pytest tests/integration/ -n auto
```

覆盖率报告包含完整的 `bsworld` Python 包，包括 runtime 调度器。

## 验证修改

已发布的镜像用构建时的可执行程序运行挂载到 `/workspace` 的 checkout：

| 修改内容 | 验证方式 |
|---|---|
| Python 框架、测试或场景 | 挂载 checkout，使用包含该套件固件的已发布镜像，参见[运行测试](../../docker/README.zh-CN.md#运行测试) |
| 固件或其构建输入（如 Zephyr shell 配置、TrouBLE 源码、UML 内核配置片段），或 Dockerfile | 用 checkout [构建](../../docker/README.zh-CN.md#构建)的镜像，或 pull request 的 CI，它会用该 checkout 构建镜像 |

已发布的镜像就是 CI 测试时的环境。本地工具或软件包不同时，本机结果可能与镜像不同，例如编译器、Python，以及 UML Guest 从主机运行的 BlueZ 和 D-Bus。本地结果与 CI 不一致时，先用同一提交的镜像 `sha-<commit>` 复现，再对比本地与镜像中的版本，例如分别运行 `gcc --version`；UML 套件可在 Linux 镜像中运行 `bluetoothctl --version`。
