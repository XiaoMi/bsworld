# UML NimBLE Linux 测试

[English](README.md) | [简体中文](README.zh-CN.md)

在 [UML Guest](../README.zh-CN.md) 中运行 Apache NimBLE 上游的 Linux Host 示例。测试将 Controller 注册为 `hci0`，用 HCI 设备编号 `0` 启动程序。Bumble Central 发现名为 `nimble` 的广播设备，连接后断开。

## 构建与运行

[UML 测试环境](../README.zh-CN.md#准备环境)使用 [Dockerfile](../../../../docker/linux.Dockerfile) 的 `nimble-linux` 阶段编译的 i386 程序。默认 Guest 路径是 `/opt/bsworld/nimble-linux`，可通过 `BSWORLD_UML_NIMBLE_BIN` 指定。

从仓库根目录运行测试。

```sh
docker run --rm --security-opt seccomp=docker/uml-seccomp.json \
  bsworld-linux:dev pytest tests/integration/uml/nimble -q
```

Guest 控制台日志和 HCI 抓包位于 `uml/nimble/`。[测试产物](../../../../docs/zh-CH/artifacts.md)
