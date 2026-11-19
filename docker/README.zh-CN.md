# BSWorld UML 测试镜像

[English](README.md) | [简体中文](README.zh-CN.md)

`ghcr.io/xiaomi/bsworld-linux` 包含 BSWorld、BabbleSim 和 32 位 UML Guest。Guest 根目录包含 Ubuntu i386 的 BlueZ、D-Bus 以及编译好的 Zephyr、NuttX 和 NimBLE 应用。镜像需要安装 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) 的 Linux/x86_64 主机。详见 [UML 测试](../tests/integration/uml/README.zh-CN.md)。

## 使用已发布镜像

从仓库根目录运行镜像内置的集成测试。Docker 中运行会启动 UML Guest 的套件时，传入 [UML seccomp 配置](uml-seccomp.json)。

```sh
sudo docker pull ghcr.io/xiaomi/bsworld-linux:latest
mkdir -p .cache/docker-results
# 已发布镜像以 UID/GID 1000 运行。
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  ghcr.io/xiaomi/bsworld-linux:latest
```

默认命令并行运行集成测试，要求执行所有选中的用例。结果写入 `.cache/docker-results`，`pytest.xml` 为测试报告。详见[测试产物](../docs/zh-CH/artifacts.md)。

### 选择版本

将 `latest` 换成 commit 或 Release tag 即可运行其他版本：

| 版本 | 镜像 tag |
|---|---|
| 最近通过测试并发布的主线版本 | `latest` |
| 完整 commit ID | `sha-<40位commit>` |
| Release tag | `v<版本号>`，例如 `v1.1.5` |

主线 CI 通过后发布 `sha-<commit>` 和 `latest`。发布 Release 时，为该提交的镜像打上 Release tag。

拉取当前检出版本对应的镜像：

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld-linux:sha-$SOURCE_COMMIT"
```

## 运行测试

从仓库根目录把检出代码挂载到 `/workspace`。优先使用该提交发布的镜像，也可以构建包含匹配的 Guest 文件系统和固件的镜像。

```sh
TEST_IMAGE="ghcr.io/xiaomi/bsworld-linux:sha-$(git rev-parse HEAD)"
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  "$TEST_IMAGE" pytest tests/integration \
    --require-integration -n auto -p no:cacheprovider \
    --out-dir /output --junitxml=/output/pytest.xml
```

修改固件、固件构建输入或 Dockerfile 后，请[构建镜像](#构建)并设置 `TEST_IMAGE=bsworld-linux:dev`。pull request 的 CI 会用修改后的代码构建镜像。

### 选择测试套件

运行单个套件时使用同一镜像。如果选中的集成用例被跳过，`--require-integration` 会让本次运行失败。

```sh
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  "$TEST_IMAGE" pytest tests/integration/uml/zephyr \
    --require-integration -n auto -p no:cacheprovider \
    --out-dir /output --junitxml=/output/zephyr-shell.xml
```

运行单元测试。

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  ghcr.io/xiaomi/bsworld-linux:latest pytest tests/unit -q -p no:cacheprovider
```

Ruff：

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  ghcr.io/xiaomi/bsworld-linux:latest ruff check --no-cache .
```

## 构建

在仓库根目录运行构建。

用 [docker-bake.hcl](docker-bake.hcl) 构建 UML 镜像。

```sh
sudo BUILD_UID="$(id -u)" BUILD_GID="$(id -g)" BUILD_JOBS=4 \
  docker buildx bake -f docker/docker-bake.hcl --load
```

镜像标签为 `bsworld-linux:dev`。`BUILD_JOBS` 控制编译并行度。

运行本地镜像：

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  bsworld-linux:dev
```

`BSWORLD_VERSION` 默认为 `0+local`，CI 从 Git 读取版本。在[测试命令](#运行测试)中把已发布镜像换成 `bsworld-linux:dev` 即可。
