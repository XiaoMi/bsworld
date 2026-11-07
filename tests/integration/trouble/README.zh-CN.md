# TrouBLE 测试

[English](README.md) | [简体中文](README.zh-CN.md)

本组测试将两个 [TrouBLE](https://github.com/embassy-rs/trouble) Rust Host 作为原生 Linux 进程运行，每个进程连接自己的 BSController（默认的[虚拟 Controller](../../../docs/zh-CH/controllers.md)），由 pytest 通过日志验证。[`source/src/bsim/`](source/src/bsim) 提供 Unix 传输、执行器和 BabbleSim 时钟。

| 程序 | 上游入口 | 可执行文件变量 |
|---|---|---|
| `ble_bas` | `trouble-example-apps` 的 `ble_bas_central` / `ble_bas_peripheral` | `BSWORLD_TROUBLE_NATIVE_BIN`，默认 `source/target/debug/ble_bas` |

## 准备环境

先按[本地 Linux 环境搭建](../../../docs/zh-CH/README.md#本地-linux)准备 BSWorld，并按[仓库构建说明](../../../docs/zh-CH/README.md#构建与测试)构建 BabbleSim。

用 [rustup](https://rustup.rs/) 安装 Rust。请在 `source/` 目录执行 Cargo，rustup 会在该目录选择并安装 [rust-toolchain.toml](source/rust-toolchain.toml) 固定的工具链。

## 构建与运行

构建程序：

```sh
cd "$BSWORLD_ROOT/tests/integration/trouble/source"
cargo build --locked
cd "$BSWORLD_ROOT"
```

运行测试：

```sh
source .venv/bin/activate
pytest tests/integration/trouble -q
```

| 文件 | 场景 |
|---|---|
| [test_trouble_native.py](test_trouble_native.py) | 广播与连接、Battery Service 发现与读取、通知 |

运行适配层单元测试：

```sh
cd "$BSWORLD_ROOT/tests/integration/trouble/source"
cargo test --locked
cd "$BSWORLD_ROOT"
```

## 添加场景

```python
central = _create_host(topo, "central")
peripheral = _create_host(topo, "peripheral")
for host in (central, peripheral):
    host.attach(topo.add_controller(log_subdir=f"trouble/{host.label}"))
await topo.start()
await peripheral.wait_log(r"\[adv\] connection established")
await central.wait_log(r"Read value: \d+")
```

`_create_host()` 定义在 [test_trouble_native.py](test_trouble_native.py) 中，角色行为在 Rust 可执行文件中实现。

进程输出位于 `trouble/<role>/`，其余见[产物目录结构](../../../docs/zh-CH/artifacts.md)。

## 许可证

- 测试代码和 `source/src/main.rs`：Apache License 2.0。
- 适配层 `source/src/bsim/`：保留原始形态的 MIT OR Apache-2.0。
- TrouBLE：MIT 或 Apache License 2.0。

分发二进制前请查阅[第三方声明](../../../docs/zh-CH/THIRD_PARTY_NOTICES.md)。
