# BSWorld Docker 测试镜像

[English](README.md) | [简体中文](README.zh-CN.md)

每个平台有各自的镜像，包含 BSWorld、测试用例和该平台编译好的固件，需要安装了 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) 的 Linux/x86_64 主机。

| 镜像 | 固件 | 默认测试套件 |
|---|---|---|
| `ghcr.io/xiaomi/bsworld-zephyr` | Zephyr Controller 与 Bluetooth shell | `tests/integration/zephyr` |
| `ghcr.io/xiaomi/bsworld-nuttx` | NuttX BTSak 与 NimBLE | `tests/integration/nuttx` |
| `ghcr.io/xiaomi/bsworld-trouble` | TrouBLE `ble_bas` | `tests/integration/trouble` |
| `ghcr.io/xiaomi/bsworld-linux` | [User Mode Linux](../tests/integration/uml/README.zh-CN.md) 内核、Ubuntu 的 BlueZ、NimBLE Linux | `tests/integration/uml` |

每个镜像都包含 Bumble，因此任一镜像都能运行 Bumble 和 BabbleSim 套件、单元测试和 Ruff。

## 使用已发布镜像

在任意目录运行镜像内置的集成测试，以 Zephyr 镜像为例：

```sh
sudo docker pull ghcr.io/xiaomi/bsworld-zephyr:latest
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest
```

默认命令用 `pytest -n auto` 运行该镜像的测试套件。结果写入 `.cache/docker-results`，`pytest.xml` 为测试报告，见[产物目录结构](../docs/zh-CH/artifacts.md)。

### 选择版本

将 `latest` 换成 commit 或 Release tag 即可运行其他版本：

| 版本 | 镜像 tag |
|---|---|
| 最近通过测试并发布的主线版本 | `latest` |
| 完整 commit ID | `sha-<40位commit>` |
| Release tag | `v1.0.0` |

每个平台的 CI 各自发布镜像：主线上测试通过后推送 `sha-<commit>` 并更新 `latest`，发布 Release 时用该 commit 的镜像打 Release tag。某个平台失败不会阻止其他平台发布，因此各平台的 `latest` 可能对应不同的 commit，而同一个 Release tag 始终对应同一个 commit。拆分为各平台镜像之前的版本发布的是单个镜像 `ghcr.io/xiaomi/bsworld`。

拉取当前检出版本对应的镜像：

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld-zephyr:sha-$SOURCE_COMMIT"
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
  ghcr.io/xiaomi/bsworld-zephyr:latest
```

固件仍来自镜像，修改固件后需要[重新构建镜像](#构建)。

### 选择测试套件

在包含该套件固件的镜像名后加 pytest 命令：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest \
  pytest tests/integration/zephyr/shell -n auto \
    --out-dir /output --junitxml=/output/zephyr-shell.xml
```

单元测试，任一镜像均可：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest pytest tests/unit -q -p no:cacheprovider
```

Ruff：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  -e PYTHONPATH=/workspace \
  ghcr.io/xiaomi/bsworld-zephyr:latest ruff check --no-cache src tests
```

## 构建

克隆包含 tag 的仓库，后续命令都在仓库根目录执行：

```sh
git clone https://github.com/XiaoMi/bsworld.git
cd bsworld
```

构建工具镜像并读取包版本：

```sh
sudo docker build --progress=plain -f docker/base.Dockerfile \
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
sudo docker build --progress=plain -f docker/base.Dockerfile \
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

用 [docker-bake.hcl](docker-bake.hcl) 构建平台镜像，它把各平台的 Dockerfile 与 [base.Dockerfile](base.Dockerfile) 中共用的阶段关联起来。可选 target 为 `zephyr`、`nuttx`、`trouble` 和 `linux`：

```sh
sudo BUILD_UID="$(id -u)" BUILD_GID="$(id -g)" BUILD_JOBS=4 \
  BSWORLD_VERSION="$BSWORLD_VERSION" \
  docker buildx bake -f docker/docker-bake.hcl --load zephyr
```

镜像标签为 `bsworld-zephyr:dev`。`BUILD_JOBS` 控制编译并行度。每个镜像有自己的 Dockerfile：

| 文件 | 构建阶段 |
|---|---|
| `base.Dockerfile` | `build-base`、`bsworld`（Python 项目依赖）、`python-test`（已安装的项目和测试）、`babblesim`、`runtime-base`（运行库和 BabbleSim） |
| `zephyr.Dockerfile` | `zephyr-workspace`（Zephyr workspace 和 SDK）、`zephyr-controller`、`zephyr-shell`、`runtime-zephyr` |
| `nuttx.Dockerfile` | `nuttx`、`runtime-nuttx` |
| `trouble.Dockerfile` | `trouble`、`runtime-trouble` |
| `linux.Dockerfile` | `uml`（内核和随机种子库）、`nimble-linux`、`runtime-linux` |

运行本地镜像：

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  -e PYTHONPATH=/workspace \
  bsworld-zephyr:dev
```

没有 Git 元数据时直接传入 `BSWORLD_VERSION`，默认为 `0+local`。在[测试命令](#运行测试)中把已发布镜像换成 `bsworld-zephyr:dev` 即可。
