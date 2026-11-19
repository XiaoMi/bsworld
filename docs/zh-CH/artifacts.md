# 日志、跟踪文件与抓包

[English](../artifacts.md) | [简体中文](artifacts.md)

BSWorld 按用例保存测试产物，便于从 Guest 控制台沿 HCI 和仿真空口定位故障。

## 输出目录与测试隔离

测试会话启动时，BSWorld 会打印本次运行的输出目录。

```text
[bsworld] log dir: /tmp/run-<timestamp>/<uid>
```

输出根目录默认为 `/tmp`，可通过 `--out-dir` 或 `OUT_DIR` 指定。

```sh
pytest tests/ --out-dir=/path/to/artifacts
OUT_DIR=/path/to/artifacts pytest tests/
```

相对输出目录会在子进程启动前解析为绝对路径，因此子进程切换工作目录后仍能读取 PHY 输入文件。

BSWorld 在输出根目录下创建 `run-<timestamp>/<uid>`。同一次 pytest-xdist 会话的 worker 共用这个目录。各用例根据自己的 pytest node ID 将日志和抓包写入独立子目录。

## Docker 与 CI 结果

Docker 默认命令将 `pytest.xml` 和 `run-<timestamp>/<uid>` 目录写到容器的 `/output`。若要在主机上保留这些产物，将主机的 `.cache/docker-results` 挂载到 `/output`。挂载方法见 [Docker 测试命令](../../docker/README.zh-CN.md#运行测试)。

需要验证特定源码提交或发行版时，按 [Docker 镜像版本说明](../../docker/README.zh-CN.md#选择版本)选择对应标签。

CI 的 UML 作业在同一个镜像中运行 Guest 和 BabbleSim 集成测试。测试报告、日志、抓包和镜像信息上传到 `bsworld-results-uml-<run-id>-<attempt>`，保留 14 天。

## 目录结构

以 Bumble 扫描 NuttX 广播的用例为例，产物目录如下。

```text
/tmp/run-<timestamp>/<uid>/log/tests/integration/uml/nuttx/
    test_btsak/test_bumble_scans_nuttx_advertising/
        bsworld.log
        babblesim/
            bs_2G4_phy_v1.log
            ellisys.bttrp
        uml/nuttx/
            nuttx.log
            hci0.snoop
            radio0.pcapng
        bumble/scanner/
            hci1.snoop
            radio1.pcapng
```

## 各类产物

| 文件 | 生成者 | 主要内容 |
|---|---|---|
| `bsworld.log` | BSWorld | 拓扑启动、清理过程和告警。 |
| `babblesim/bs_2G4_phy_v1.log` | BabbleSim PHY | PHY 进程的标准输出和标准错误。正常运行时可能为空。 |
| `uml/<label>/<label>.log` | UML Guest | 控制台的标准输出和标准错误。测试可指定其他日志目录。 |
| `hciN.snoop` | BSController | Host 与 Controller 之间的 H4 报文。 |
| `radioN.pcapng` | BSController | 该 Controller 发送的报文，以及接收期间 PHY 返回的报文。 |
| `babblesim/ellisys.bttrp` | BSController | 用例中各 BSController 的 Bluetooth LE 发送记录，按仿真时间排序。 |
| `babblesim/att_matrix.txt` | BSWorld | 本测试实际传给 `multiatt` 信道模型的静态路径衰减矩阵。 |
| `babblesim/timed_att_*.txt` | BSWorld | 时变路径的时间点和衰减值。 |

`bsworld.log` 使用主机的墙钟时间，可用于分析启动和清理耗时。测量协议时序时，使用同一仿真时钟的时间戳，例如 HCI 抓包中的时间戳。

## 业务程序日志与 `wait_log()`

BSWorld 将 Guest 控制台输出写入 `.log` 文件。测试通过 `UmlGuest.wait_log()` 匹配日志。

```python
await device.wait_log(r"Bluetooth initialized", timeout=30)
```

`wait_log()` 按到达顺序对完整行做正则匹配。每次调用会消耗已扫描的行。调用前到达且尚未扫描的日志仍可匹配。未收到换行符的内容会先写入文件，形成完整行后才参与匹配。

拓扑启动完成后，`timeout` 以仿真时间计算。

将 `TEST_LOG_DIR` 设为上例中该用例的输出目录，即可实时查看 NuttX 控制台。

```sh
tail -f "${TEST_LOG_DIR:?}/uml/nuttx/nuttx.log"
```

## 框架日志

从 `topo.start()` 开始，BSWorld 将下列信息写入 `bsworld.log`。

- BabbleSim 实例标识和设备数量。
- PHY 和 UML Guest 的启动命令。
- 每个进程的工作目录、日志路径和 PID。
- Controller HCI socket 和 BabbleSim 设备锁的就绪状态。
- 清理阶段仍在运行的进程及停止顺序。

受管理的进程在清理前以非零状态退出时，BSWorld 会记录退出状态和该进程的最后几行输出。

## HCI BTSnoop 抓包

每个 Controller 记录收发的 HCI 报文，使用仿真时间戳。抓包采用 BTSnoop v1 格式和 H4 链路类型 1002，记录完整报文长度和方向。

将 `TEST_LOG_DIR` 设为上例的用例目录后，可用 `btmon` 或 [Wireshark](https://www.wireshark.org/) 打开 Bumble 抓包。

```sh
wireshark "${TEST_LOG_DIR:?}/bumble/scanner/hci1.snoop"
btmon -r "${TEST_LOG_DIR:?}/bumble/scanner/hci1.snoop"
```

Controller 默认将两个抓包文件写入 `controllers/hciN/`。添加 Controller 时可通过 `log_subdir` 将抓包与 Guest 或 Bumble 的产物放在一起。

## Controller 空口抓包

`radioN.pcapng` 记录该 Controller 发送的报文和接收期间 PHY 返回的报文。每条记录包含仿真时间戳和收发方向。接收记录还包含可用的 RSSI 和 CRC 结果。

用 Wireshark 打开抓包即可对照该 Controller 的发送与接收活动。

```sh
wireshark "${TEST_LOG_DIR:?}/bumble/scanner/radio1.pcapng"
```

## Ellisys 空口跟踪文件

BSWorld 以 `-nodump` 启动 PHY。各 BSController 提供自己的发送记录，测试清理时将这些记录按仿真时间写入共享文件 `babblesim/ellisys.bttrp`。

在 [Ellisys](https://www.ellisys.com/products/bex400/) 中选择 **File > Import** 和 **Bluetooth packets**，再通过 **Next** 和 **Browse** 打开 `ellisys.bttrp`。

<p align="center">
  <img src="../images/ellisys-trace-import.gif" alt="导入 BSWorld Ellisys 跟踪文件" width="760">
</p>

跟踪文件可用于检查各 Controller 已发送的报文、发送信道和时间，以及多次发送是否重叠。Ellisys 记录使用固定的 RSSI。排查接收失败时，可对照接收端的 `radioN.pcapng` 与测试配置中的衰减、干扰设置。

## 失败排查顺序

1. 在 `bsworld.log` 中检查启动命令、HCI socket 和设备锁的就绪状态，以及是否有进程提前退出。
2. 在 Guest 控制台日志中查看程序最后报告的状态，确认 `wait_log()` 等待的日志行是否出现。
3. 在 `hciN.snoop` 中检查预期的 Host 命令和 Controller 事件。
4. 在 `radioN.pcapng` 中检查单个 Controller 的发送和接收，在 `ellisys.bttrp` 中检查各 Controller 的发送时序。
5. 无线路径不符合预期时，对照生成的 `att_matrix.txt` 和 `timed_att_*.txt` 检查设备编号、方向和生效时间。

BSWorld 在输出目录中保留每个用例的日志和抓包。清理时移除临时 BabbleSim 状态、socket 和已有的原始结果。
