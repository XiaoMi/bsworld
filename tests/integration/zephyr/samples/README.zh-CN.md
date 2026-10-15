# Zephyr Bluetooth 示例测试

[English](README.md) | [中文](README.zh-CN.md)

这些测试将未经修改的 Zephyr Bluetooth 示例程序作为独立的 Host 进程接入 BSWorld。公共配置和 devicetree overlay 让每个 Host 通过 Unix socket 连接到独立的虚拟 Controller。

| 测试目录 | Zephyr 示例程序 | 覆盖内容 |
|---|---|---|
| [broadcast_observe/](broadcast_observe/README.zh-CN.md) | `broadcaster`、`observer` | 广播、被动扫描，以及覆盖三个广播信道的定向干扰与恢复 |
| [heart_rate/](heart_rate/README.zh-CN.md) | `peripheral_hr`、`central_hr` | 连接、GATT 发现和通知 |
| [periodic_advertising/](periodic_advertising/README.zh-CN.md) | `periodic_adv`、`periodic_sync` | 周期广播同步 |
| [mesh/](mesh/README.zh-CN.md) | `mesh`、`mesh_provisioner` | Mesh 配网和配置模型控制 |

每个目录维护自己的 Python 测试、进程配置和可选配置片段。

## 环境准备

推荐使用[已发布的 Docker 镜像](../../../../docker/README.zh-CN.md#运行测试)，其中已包含全部八个 Zephyr 示例。选择 Zephyr 套件时使用 `pytest tests/integration/zephyr/samples`。以下准备步骤用于本机 Linux 构建。

先按照 [Zephyr 工作区说明](common/source/README.zh-CN.md)准备源码和 SDK，并按[README 构建说明](../../../../docs/zh-CH/README.md#构建与测试)编译 BabbleSim，再按[虚拟 Controller README](../../../../components/bt_virt_controller/README.zh-CN.md)编译 Controller。

保留 `BSWORLD_ROOT`、`ZEPHYR_WORKSPACE`、`BSIM_OUT_PATH`、`BSIM_COMPONENTS_PATH` 和 `BSIM_BASE_PATH`，以及测试使用的 `BSWORLD_BSIM_DIR`、`BSWORLD_BT_VIRT_CONTROLLER_BIN`。将这些变量设为本机工作区的路径。

## 构建与运行

各测试目录的 README 分别记录本组示例的 `west build` 命令、原始产物路径、环境变量和 pytest 命令，见上表链接。构建及测试均在同一个 shell 中执行，切换 Python 环境时保留导出的变量。

完成全部组的编译并导出变量后，可以在 BSWorld Python 环境中统一运行：

```sh
cd "$BSWORLD_ROOT"
source .venv/bin/activate
pytest tests/integration/zephyr/samples -q --no-ellisys
```
