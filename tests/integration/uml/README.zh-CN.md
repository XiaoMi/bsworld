# User Mode Linux 测试环境

[English](README.md) | [简体中文](README.zh-CN.md)

BSWorld 在 64 位 Linux 上运行 32 位 User Mode Linux（UML）Guest。每个 Guest 中的 Host 通过独立的 BSController 与 pytest 中的 Bumble 对测。同一拓扑可以运行多个 Guest，共享仿真时钟。

## 准备环境

### Docker

在仓库根目录构建 Linux 测试镜像并运行 UML 套件。挂载后，日志和抓包保存在 `.cache/docker-results`。

```sh
docker buildx bake -f docker/docker-bake.hcl --load linux
mkdir -p .cache/docker-results
sudo chown 1000:1000 .cache/docker-results
docker run --rm \
  --security-opt seccomp=docker/uml-seccomp.json \
  --mount "type=bind,src=$PWD/.cache/docker-results,dst=/output" \
  bsworld-linux:dev pytest tests/integration/uml --require-integration -n auto -q \
    --out-dir /output/uml --junitxml=/output/uml.xml
```

单独运行某个 Host 时，用下表中的路径替换 `tests/integration/uml`。如需挂载本地工作区并使用指定提交的镜像，命令见 [Docker 指南](../../../docker/README.zh-CN.md#运行测试)。

### 本地 Linux

先按[本地 Linux 指南](../../../docs/zh-CH/README.md#本地-linux)准备 BSWorld 和 BabbleSim。在 Ubuntu 24.04 上启用 i386 软件包并安装内核构建工具。

```sh
sudo dpkg --add-architecture i386
sudo apt-get update
sudo apt-get install -y bc bison flex

UML_DIR="$BSWORLD_ROOT/.cache/uml"
LINUX_DIR="$UML_DIR/linux-src"
mkdir -p "$UML_DIR"

export BSWORLD_UML_KERNEL="$LINUX_DIR/linux"
export BSWORLD_UML_GETRANDOM="$UML_DIR/getrandom.so"
export BSWORLD_UML_ROOTFS="$UML_DIR/rootfs"
```

| 环境变量 | 宿主机路径 |
|---|---|
| `BSWORLD_UML_KERNEL` | 可执行的 i386 UML 内核 |
| `BSWORLD_UML_GETRANDOM` | UML 进程加载的 i386 种子库 |
| `BSWORLD_UML_ROOTFS` | 包含 `/init`、i386 程序和库的 Guest 根目录 |

#### 编译内核和种子库

获取 [Dockerfile](../../../docker/linux.Dockerfile) 固定的 Linux 版本，编译 i386 UML 内核和种子库。

```sh
LINUX_REVISION=$(sed -n 's/^ARG LINUX_REVISION=//p' "$BSWORLD_ROOT/docker/linux.Dockerfile")
git init "$LINUX_DIR"
git -C "$LINUX_DIR" fetch --depth=1 https://github.com/torvalds/linux.git "${LINUX_REVISION:?}"
git -C "$LINUX_DIR" checkout --detach FETCH_HEAD

(
  cd "${LINUX_DIR:?}" &&
  make ARCH=um SUBARCH=i386 defconfig &&
  scripts/kconfig/merge_config.sh -m .config "$BSWORLD_ROOT/src/bsworld/uml/linux.fragment" &&
  make ARCH=um SUBARCH=i386 olddefconfig &&
  make ARCH=um SUBARCH=i386 -j"$(nproc)"
) && cc -m32 -shared -fPIC -O2 -o "${BSWORLD_UML_GETRANDOM:?}" \
  "$BSWORLD_ROOT/src/bsworld/uml/getrandom.c"
```

#### 组装 Guest 根文件系统

用当前用户运行[根文件系统脚本](../../../docker/uml-rootfs.sh)，解包 Ubuntu i386 运行时软件包，并把 UML 启动脚本安装为 `/init`。

```sh
sh "$BSWORLD_ROOT/docker/uml-rootfs.sh" "${BSWORLD_UML_ROOTFS:?}" &&
install -m 0755 "$BSWORLD_ROOT/src/bsworld/uml/init.sh" "$BSWORLD_UML_ROOTFS/init"
```

按下表对应的 Host 指南准备要跑的套件。如需替换程序，覆盖变量使用 **Guest 内的绝对路径**。例如，程序安装到 `$BSWORLD_UML_ROOTFS/opt/bsworld/my-shell` 时，设置 `BSWORLD_UML_ZEPHYR_BIN=/opt/bsworld/my-shell`。

## 测试套件

| Host 指南 | 测试入口 |
|---|---|
| [BlueZ](bluez/README.zh-CN.md) | `tests/integration/uml/bluez` |
| [Zephyr 蓝牙 shell](zephyr/README.zh-CN.md) | `tests/integration/uml/zephyr` |
| [NuttX BTSak 和 NimBLE](nuttx/README.zh-CN.md) | `tests/integration/uml/nuttx` |
| [Apache NimBLE Linux](nimble/README.zh-CN.md) | `tests/integration/uml/nimble` |

准备好所有 Host 后，在 BSWorld 的虚拟环境中运行全套 UML 测试。

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/uml --require-integration -n auto -q
```

## Guest 运行机制

Guest 从只读根文件系统启动。`/init` 挂载可写的运行目录并启动 shell，测试通过 UML 进程的标准输入和输出与 shell 交互。启动步骤见 [init.sh](../../../src/bsworld/uml/init.sh)。

每个 Guest 通过 `/dev/ttyS0` 上的 H4 连接接入对应的 Controller。测试使用 `btattach -B /dev/ttyS0 -P h4` 将 Controller 注册为 `hci0`。

pytest 的事件循环通过 `time-travel=ext` 推进各 Guest 的时钟。Guest 运行时仿真时间暂停，控制台和串口输入在 Guest 的周期 tick 到来时处理。BabbleSim 设备编号分配给 Controller、测试时钟和配置的干扰源。

## 添加场景

在 `await topo.start()` 前为每个 Guest 添加一个 Controller。启动拓扑后，在 `AsyncExitStack` 中调用 `open_uml_guest()`，为每个 Guest 指定独立的标签。退出 `AsyncExitStack` 时，Guest 会在 pytest 清理拓扑前关闭。相关函数见 [common.py](common.py)。

用 `guest.shell.run()` 发命令，再用 `guest.wait_log()` 检查控制台输出。

启动器和时钟调度代码位于 [bsworld.uml](../../../src/bsworld/uml)。运行 `pytest tests/unit/uml -q` 可检查其[单元测试](../../unit/uml)，无需构建 UML 内核和 BabbleSim。

## 许可证

测试与 BSWorld 的 UML 适配代码使用 Apache License 2.0。Linux 和 Guest 程序的许可证见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
