# BSWorld UML 测试镜像

[English](README.md) | 简体中文

`ghcr.io/xiaomi/bsworld-linux` 包含 BSWorld、BabbleSim 和 32 位 UML 测试环境。Guest 内置程序及对应测试见 [UML 测试环境指南](../tests/integration/uml/README.zh-CN.md)。

## 使用已发布镜像

在 Linux/x86_64 主机上安装 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/)，然后在仓库根目录加载 [UML seccomp 配置](uml-seccomp.json)并运行集成测试。

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

默认命令并行运行集成测试。如果有选中的用例被跳过，本次运行会失败。

`pytest.xml`、日志和抓包位于 `.cache/docker-results`。完整目录结构见[测试产物说明](../docs/zh-CH/artifacts.md)。

### 选择版本

| 版本 | 镜像 tag |
|---|---|
| 最近通过测试并发布的主线版本 | `latest` |
| 完整 commit ID | `sha-<40位commit>` |
| Release tag | `v<版本号>`，例如 `v1.1.5` |

主线提交通过测试后，CI 会发布 `sha-<commit>`。如果该提交仍是主线的最新提交，CI 也会更新 `latest`。发布 Release 时，将 Git 标签添加到该提交已通过测试的镜像上。

拉取当前检出版本对应的镜像。

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
sudo docker pull "ghcr.io/xiaomi/bsworld-linux:sha-$SOURCE_COMMIT"
```

## 运行测试

在仓库根目录运行以下命令，将检出代码挂载到 `/workspace`，并选用同一提交对应的镜像。

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

修改 Guest 可执行文件、UML Virtio 后端、构建输入或 Dockerfile 后，先[构建本地镜像](#构建)，再设置 `TEST_IMAGE=bsworld-linux:dev`。

### 选择测试套件

将测试套件目录传给 pytest 即可单独运行。若选中的集成用例被跳过，`--require-integration` 会让本次运行失败。

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

用 Ruff 检查代码。

```sh
sudo docker run --rm \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  ghcr.io/xiaomi/bsworld-linux:latest ruff check --no-cache .
```

## 构建

在仓库根目录运行 Buildx Bake。`BUILD_UID` 和 `BUILD_GID` 设置镜像内用户的 UID 和 GID，`BUILD_JOBS` 控制编译并行度。构建参数见 [Bake 配置](docker-bake.hcl)。

```sh
sudo BUILD_UID="$(id -u)" BUILD_GID="$(id -g)" BUILD_JOBS=4 \
  docker buildx bake -f docker/docker-bake.hcl --load
```

构建完成后，使用生成的 `bsworld-linux:dev` 镜像运行默认集成套件。

```sh
mkdir -p .cache/docker-results
sudo chown "$(id -u):$(id -g)" .cache/docker-results
sudo docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  bsworld-linux:dev
```

可以用 `BSWORLD_VERSION` 指定包版本。本地构建默认为 `0+local`，CI 从 Git 获取版本。
