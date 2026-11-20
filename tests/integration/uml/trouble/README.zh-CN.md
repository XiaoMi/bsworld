# UML TrouBLE 测试

[English](README.md) | [简体中文](README.zh-CN.md)

套件在 32 位 UML Guest 中运行 TrouBLE 上游的 Linux Battery Service 示例。示例通过 Linux HCI User Channel 打开 Guest 的 `hci0`。

## 构建与运行

Linux 测试镜像包含两个可执行文件。在 [Docker 单套件命令](../../../../docker/README.zh-CN.md#选择测试套件)中选择 `tests/integration/uml/trouble`。

| 角色 | Guest 内默认可执行文件 | 覆盖变量 |
|---|---|---|
| Central | `/opt/bsworld/trouble/ble_bas_central` | `BSWORLD_UML_TROUBLE_CENTRAL_BIN` |
| Peripheral | `/opt/bsworld/trouble/ble_bas_peripheral` | `BSWORLD_UML_TROUBLE_PERIPHERAL_BIN` |

需要更换可执行文件时，将覆盖变量设为 Guest 内的绝对路径。

本地构建前，准备 [UML 环境](../README.zh-CN.md#本地-linux)并安装 `rustup`。使用 Rust 1.97.1 i686 工具链和 [Dockerfile](../../../../docker/linux.Dockerfile) 固定的 TrouBLE 版本编译，并安装 Linux 示例。

```sh
rustup toolchain install 1.97.1 --profile minimal --target i686-unknown-linux-gnu
TROUBLE_DIR="$BSWORLD_ROOT/.cache/trouble"
TROUBLE_REVISION=$(sed -n 's/^ARG TROUBLE_REVISION=//p' "$BSWORLD_ROOT/docker/linux.Dockerfile")
git init "$TROUBLE_DIR"
git -C "$TROUBLE_DIR" fetch --depth=1 https://github.com/embassy-rs/trouble.git "${TROUBLE_REVISION:?}"
git -C "$TROUBLE_DIR" checkout --detach FETCH_HEAD

(
  cd "$TROUBLE_DIR/examples/linux" &&
  CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 \
    cargo +1.97.1 build --locked --release --target i686-unknown-linux-gnu \
      --bin ble_bas_central --bin ble_bas_peripheral &&
  install -Dm755 -t "${BSWORLD_UML_ROOTFS:?}/opt/bsworld/trouble" \
    target/i686-unknown-linux-gnu/release/ble_bas_central \
    target/i686-unknown-linux-gnu/release/ble_bas_peripheral
)
```

在 BSWorld 虚拟环境中，从仓库根目录运行套件。

```sh
pytest tests/integration/uml/trouble --require-integration -q
```

## 测试覆盖

[test_trouble.py](test_trouble.py) 覆盖两种 Host 角色下的 Battery Service。

- TrouBLE 作为 Peripheral 时，Bumble 连接并发现 Battery Service，读取 Battery Level 并接收通知。
- TrouBLE 作为 Central 时，TrouBLE 示例连接 Bumble，完成同样的服务发现、读取和通知流程。

## 测试产物

Guest 控制台日志为 `uml/trouble/trouble.log`，TrouBLE 的 Controller 抓包与日志位于同一目录。Bumble 的抓包位于 `bumble/central/` 或 `bumble/peripheral/`。用例输出目录和文件格式见[日志、跟踪文件与抓包](../../../../docs/zh-CH/artifacts.md)。
