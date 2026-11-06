# User Mode Linux 测试环境

[English](README.md) | [简体中文](README.zh-CN.md)

在 User Mode Linux（UML）中运行 Linux Guest。UML 是编译成普通主机程序的主线 Linux 内核，Guest 直接运行主机上安装的 BlueZ。Guest 的 `/dev/ttyS0` 连接到虚拟 Controller，Guest 时钟通过 UML time-travel 与 BabbleSim 仿真时间同步。

## 已验证版本

| 组件 | 提交或版本 |
|---|---|
| Linux | `7.4-rc1`（`f66469d9905b39d2c260a15d7d6db450f4db67c1`） |
| BlueZ（主机） | `bluetoothd` 5.86，`bluetoothctl` 5.64 |

## 准备环境

先按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim 和[虚拟 Controller](../../../docs/zh-CH/controllers.md#编译-zephyr-controller)。

Guest 以只读方式挂载主机的根文件系统并运行其中的程序，因此主机需要安装 64 位的 `bluetoothd`、`bluetoothctl`、`btattach` 和 `dbus-daemon`。64 位 UML 内核不能运行 32 位程序，可以用 `file /usr/bin/dbus-daemon` 检查。

获取 Linux：

```sh
export UML_DIR="$HOME/bsworld-uml"
export LINUX_DIR="$UML_DIR/linux"
git init "$LINUX_DIR"
git -C "$LINUX_DIR" fetch --depth 1 https://github.com/torvalds/linux.git f66469d9905b39d2c260a15d7d6db450f4db67c1
git -C "$LINUX_DIR" checkout --detach FETCH_HEAD
```

## 编译 Linux Guest

用 [linux.fragment](source/linux.fragment) 配置并编译内核：

```sh
cd "$LINUX_DIR"
make ARCH=um defconfig
scripts/kconfig/merge_config.sh -m .config "$BSWORLD_ROOT/tests/integration/uml/source/linux.fragment"
make ARCH=um olddefconfig
make ARCH=um -j"$(nproc)"
```

UML 启动时从主机获取 Guest 随机数生成器的种子。编译 [getrandom.c](source/getrandom/getrandom.c)，用固定种子代替它，使随机地址和密钥每次运行都相同：

```sh
cc -shared -fPIC -O2 -o "$UML_DIR/getrandom.so" \
  "$BSWORLD_ROOT/tests/integration/uml/source/getrandom/getrandom.c"
```

导出产物：

```sh
export BSWORLD_UML_KERNEL="$LINUX_DIR/linux"
export BSWORLD_UML_GETRANDOM="$UML_DIR/getrandom.so"
```

检查产物：

```sh
test -x "$BSWORLD_UML_KERNEL"
test -f "$BSWORLD_UML_GETRANDOM"
```

## 运行时数据路径

测试把虚拟 Controller 的 socket 作为 Guest 的 `/dev/ttyS0`，并用 `btattach -B /dev/ttyS0 -P h4` 注册 `hci0`。这条串口实际是 socket，不需要设置波特率和流控。Guest 控制台就是进程的 stdin 和 stdout。[init.sh](source/init.sh) 在 `/tmp`、`/run` 和 `/var/lib/bluetooth` 上挂载内存文件系统，用 [main.conf](source/main.conf) 启动 D-Bus 和 `bluetoothd`，然后打开 root shell。

内核以 `time-travel=ext` 启动，时钟的每次推进都由 pytest event loop 批准，Guest 运行期间仿真时间不前进。串口和控制台无法唤醒等待中的 Guest，因此内核使用周期 tick：输入在下一个 tick 处理，最多晚 10 ms（HZ=100）。

Guest 的随机种子、起始时间（2026-01-01T00:00:00Z）和内核命令行固定，因此 Guest 日志、HCI 抓包和空口日志每次运行都相同。内核会把命令行混入随机数池，所以 Controller socket 总是以进程允许的最大描述符编号交给 Guest（`ssl0=fd:N`，N 为 `ulimit -n` 减 1）。

## 运行测试

见 [BlueZ](bluez/README.zh-CN.md)。

## 重建配置

修改 [linux.fragment](source/linux.fragment) 后，在 `$LINUX_DIR` 中重新执行上面的配置和编译命令。

## 添加场景

在 [BlueZ](bluez/README.zh-CN.md) 目录中添加异步 pytest 测试。启动前通过 [common.py](common.py) 中的 `add_uml_controller(topo, ...)` 登记 Controller。`topo.start()` 之后，在 `AsyncExitStack` 中调用 `open_uml_guest(stack, topo, controller, ...)` 启动 Guest，stack 关闭时 Guest 随之停止。再通过 `guest.shell` 发送命令并用 `wait_log()` 验证。

## 许可证

- 测试和 `source/` 中的文件：Apache License 2.0。
- Linux 及 Guest 运行的主机软件：保留各自的许可证。

详见[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
