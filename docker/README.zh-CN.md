# BSWorld Docker 测试镜像

[English](README.md) | [简体中文](README.zh-CN.md)

镜像包含 BSWorld、测试用例和编译好的固件，需要安装了 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) 的 Linux/x86_64 主机。[User Mode Linux 测试](../tests/integration/uml/README.zh-CN.md)需要在本地编译内核，在镜像中会被跳过。

## 使用已发布镜像

在任意目录运行镜像内置的集成测试：

```sh
sudo docker pull ghcr.io/xiaomi/bsworld:latest
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

默认命令为 `pytest tests/integration -n 2`。结果写入 `.cache/docker-results`，`pytest.xml` 为测试报告，见[产物目录结构](../docs/zh-CH/artifacts.md)。

### 选择版本

将 `latest` 换成 commit 或 Release tag 即可运行其他版本：

| 版本 | 镜像地址 |
|---|---|
| 最近通过测试并发布的主线版本 | `ghcr.io/xiaomi/bsworld:latest` |
| 完整 commit ID | `ghcr.io/xiaomi/bsworld:sha-<40位commit>` |
| Release tag | `ghcr.io/xiaomi/bsworld:v1.0.0` |

拉取当前检出版本对应的镜像：

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld:sha-$SOURCE_COMMIT"
```

## 运行测试

测试本地代码时，在仓库根目录把仓库挂载到 `/workspace`：

```sh
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest
```

固件仍来自镜像，修改固件后需要[重新构建镜像](#构建)。

### 选择测试套件

在镜像名后加 pytest 命令选择套件：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest \
  pytest tests/integration/zephyr/shell -n 2 \
    --out-dir /output --junitxml=/output/zephyr-shell.xml
```

单元测试：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest pytest tests/unit -q -p no:cacheprovider
```

Ruff：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld:latest ruff check --no-cache src tests
```

## 构建

克隆包含 tag 的仓库，后续命令都在仓库根目录执行：

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
```

构建工具镜像并读取包版本：

```sh
sudo docker build --progress=plain -f docker/Dockerfile \
  --target bsworld -t bsworld:tooling \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" .
BSWORLD_VERSION="$(sudo docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 --env GIT_OPTIONAL_LOCKS=0 \
  bsworld:tooling python -m setuptools_scm)"
```

构建 `python-test` 并运行 lint 和单元测试：

```sh
sudo docker build --progress=plain -f docker/Dockerfile \
  --target python-test \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" \
  --build-arg BSWORLD_VERSION="$BSWORLD_VERSION" \
  -t bsworld:python-test .
sudo docker run --rm --env PYTHONDONTWRITEBYTECODE=1 \
  bsworld:python-test python -m ruff check --no-cache src tests
sudo docker run --rm --env PYTHONDONTWRITEBYTECODE=1 \
  bsworld:python-test python -m pytest tests/unit -q -p no:cacheprovider
```

构建运行镜像：

```sh
sudo docker build --progress=plain -f docker/Dockerfile \
  --target runtime \
  --build-arg BUILD_UID="$(id -u)" \
  --build-arg BUILD_GID="$(id -g)" \
  --build-arg BUILD_JOBS=4 \
  --build-arg BSWORLD_VERSION="$BSWORLD_VERSION" \
  -t bsworld:dev .
```

`BUILD_JOBS` 控制编译并行度。构建阶段：

```text
build-base
├── bsworld（Python 项目依赖）
│   └── python-test（已安装的项目和测试）
│       └── runtime（加入运行库和下方各分支的固件）
├── babblesim
│   ├── zephyr
│   ├── nuttx
│   └── trouble
```

运行本地镜像：

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  bsworld:dev
```

没有 Git 元数据时直接传入 `BSWORLD_VERSION`，默认为 `0+local`。在[测试命令](#运行测试)中把已发布镜像换成 `bsworld:dev` 即可。
