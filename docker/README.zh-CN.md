# BSWorld Docker 测试镜像

[English](README.md) | [中文](README.zh-CN.md)

推荐拉取 `ghcr.io/xiaomi/bsworld:latest`，直接在 Linux/x86_64 上运行测试。也可以通过 [Dockerfile](Dockerfile) 自行构建镜像。

## 镜像内容

[Dockerfile](Dockerfile) 按 BSWorld、BabbleSim、Zephyr、NuttX、QEMU、Buildroot 的顺序准备依赖和编译产物，最后生成独立的 `runtime` 镜像。

最终镜像包含 Python 3.12、BSWorld、pytest、测试用例、BabbleSim 运行组件及 Ellisys 转换脚本、虚拟 Controller、八个 Zephyr 示例、BTSak / NimBLE 两个 NuttX 程序，以及 QEMU、Linux kernel 和 guest rootfs。固件路径已通过镜像环境变量配置。

最终镜像包含运行所需的程序和库。上游源码、SDK、工具链和中间文件保存在 Docker 构建缓存中。

## 前置条件

使用 Linux/x86_64 主机并安装 Docker Engine。Ubuntu 安装步骤见 Docker 官方[安装文档](https://docs.docker.com/engine/install/ubuntu/)，安装后检查：

```sh
sudo docker info
```

测试当前仓库代码需要 Git；自行构建镜像还需要 Docker 的 Buildx 插件。

## 使用已发布镜像

在任意目录拉取镜像并运行其中的集成测试：

```sh
sudo docker pull ghcr.io/xiaomi/bsworld:latest
mkdir -p .cache/docker-results
# The published image runs as UID/GID 1000.
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

`latest` 指向最近从 `main` 发布且通过测试的环境。需要使用指定环境时，将它替换为对应 CI 运行摘要中的 `env-<hash>` 标签。

镜像已包含 Python、BSWorld、测试用例和编译好的固件。默认命令为 `pytest tests/integration -n 2 --out-dir /output --junitxml=/output/pytest.xml`。

## 运行测试

### 测试当前仓库代码

首次使用时先获取仓库。在仓库根目录执行以下命令，使用镜像内的固件测试自己的 Python 代码和测试用例：

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

挂载 `/workspace` 后，使用的是当前仓库的 Python 代码和测试用例。固件位于镜像内的 `/opt`；修改固件源码或构建配置后，需要重新构建镜像。

### 选择测试套件

在仓库根目录执行，将 pytest 命令写在镜像名之后，例如：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest \
  pytest tests/integration/zephyr/samples/heart_rate -n 2 \
    --out-dir /output --junitxml=/output/heart-rate.xml
```

### 单元测试和静态检查

检查当前仓库代码：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest pytest tests/unit -q -p no:cacheprovider
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest ruff check --no-cache src tests
```

## 构建

首次使用时先获取仓库，然后在仓库根目录执行：

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
sudo docker buildx version
sudo docker build --progress=plain -f docker/Dockerfile \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" \
  --build-arg BUILD_JOBS=4 \
  -t bsworld:dev .

mkdir -p .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  bsworld:dev
```

| 参数 | 用途 | 默认值 |
|---|---|---|
| `BUILD_UID` / `BUILD_GID` | 镜像内 `bsworld` 用户的 UID/GID，本地构建时与当前用户一致 | `1000` / `1000` |
| `BUILD_JOBS` | 固件编译并行度 | `4` |

源码版本和 SDK 选项在 Dockerfile 中配置。首次构建会下载源码、SDK 并编译全部固件；后续构建复用 Docker 层缓存。镜像构建过程中会检查二进制文件及其运行库依赖。

使用本地镜像执行[测试命令](#运行测试)时，将 `ghcr.io/xiaomi/bsworld:latest` 替换为 `bsworld:dev`。本地构建采用当前用户的 UID/GID，输出目录由当前用户持有即可。

## 文件与日志

| 容器路径 | 内容 |
|---|---|
| `/workspace` | BSWorld Python 代码和测试，可挂载本次提交 |
| `/opt/firmware` | Controller、Zephyr、NuttX、QEMU、kernel、rootfs |
| `/opt/babblesim` | BabbleSim 程序、动态模型及转换脚本 |
| `/opt/venv` | BSWorld Python 环境 |
| `/output` | JUnit 报告和按测试生成的日志、抓包文件 |

上述命令把 `/output` 挂载到本机 `.cache/docker-results`。目录需要允许镜像内的 `bsworld` 用户写入；本地构建命令通过 UID/GID 参数保持一致，已发布镜像的示例则为 UID/GID 1000 准备目录权限。CI workflow 会自动准备其结果目录权限。

pytest 的终端输出直接显示在 Docker 调用方的终端或 CI 步骤日志中。默认命令将 pytest 的退出码传递给 `docker run`，CI 据此判定测试成功或失败。workflow 在测试结束后上传结果目录，测试失败时也保留报告与日志。

### 容器终端

在仓库根目录打开容器终端，挂载当前仓库：

```sh
sudo docker run --rm -it \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest bash
```

本机 Linux 构建见[仓库 README](../docs/zh-CH/README.md#本地-linux)；测试日志结构见[日志文档](../docs/zh-CH/artifacts.md)，第三方组件说明见[第三方声明](../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
