# 为 BSWorld 贡献

[English](../../CONTRIBUTING.md) | [中文](CONTRIBUTING.md)

感谢参与 BSWorld。项目欢迎缺陷修复、测试、文档、新的 Bluetooth LE 业务程序环境、平台接入，以及对确定性运行时和 RF 编排能力的改进。

BSWorld 自有代码使用 Apache License 2.0。引入或复用的第三方组件继续遵循原许可证，并在源码和[第三方声明](THIRD_PARTY_NOTICES.md)中明确标识。所有提交都使用 [Developer Certificate of Origin](https://developercertificate.org/) 流程，通过 `Signed-off-by` 记录贡献者的授权声明。

## 开始之前

- 提交新 Issue 前，先检索是否已有相同问题。
- 安全漏洞请按照 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中披露敏感细节。
- 大范围架构调整、新后端或公共接口变更，请先提交 Issue 讨论设计和边界。
- 小范围修复、针对性测试和文档勘误可以直接提交 Pull Request。
- 在项目的所有交流空间中遵守[社区行为准则](CODE_OF_CONDUCT.md)。

## 搭建开发环境

推荐使用 [Docker 测试镜像](../../docker/README.zh-CN.md#运行测试)，将仓库挂载到 `/workspace`，执行静态检查、单元测试和集成测试。修改固件或构建配置时，[自行构建镜像](../../docker/README.zh-CN.md#构建)。

## 修改代码

- 每项修改只解决一个明确问题，避免夹带无关重构或生成文件变化。
- 保持现有职责边界：可复用框架代码放在 `src/bsworld/`，业务程序专用的进程配置、构建文件和测试场景放在 `tests/`。
- 异步测试可以直接使用标准 asyncio API，但不能调用 `time.sleep()` 这类会阻塞事件循环的等待接口。
- 对外可见行为发生变化时，需要增加或更新测试。测试应优先断言公开结果，而不是绑定私有实现细节。
- 行为、命令或公共接口发生变化时，中英文文档必须同步更新。
- 不要提交本地编译产物、缓存或测试日志。
- 新增的一方源码、配置和脚本应使用相邻文件中的 Apache-2.0 版权头。第三方代码必须保留原有许可证声明；引入新的外部组件时，需要同步更新 `THIRD_PARTY_NOTICES*`。

## 验证修改

以下检查在挂载当前仓库的 [Docker 终端](../../docker/README.zh-CN.md#容器终端)或本机 Python 环境中执行。框架代码至少应通过：

```sh
ruff check --no-cache src tests
pytest tests/unit/
```

修改进程启动、调度、HCI、RF 行为、平台配置或测试业务程序时，还需要运行受影响的虚拟系统测试，例如：

```sh
pytest tests/integration/nuttx/btsak -n auto
pytest tests/integration/nuttx/nimble -n auto
pytest tests/integration/zephyr/samples -n auto
pytest tests/integration/bumble -n auto
```

修改跨越多个子系统时，运行完整测试：

```sh
pytest tests/ -n auto
```

如果本地无法运行某项必要测试，请在 Pull Request 中明确说明未运行的内容和原因。

## 提交 Pull Request

Pull Request 应当：

- 说明问题以及修改的必要性；
- 描述修改后的行为和兼容性影响；
- 列出实际执行的验证命令和结果；
- 根据需要同步提交测试和文档；
- 说明新增依赖、复制代码、生成资源或许可证变化；
- 不包含凭据、私有日志、构建机绝对路径或无关二进制文件。

较大的修改可能需要拆分成多个便于审核的提交。提交信息应简洁、使用祈使语气，并描述对外有意义的变化，而不是记录编辑过程。

## 开发者原创声明

BSWorld 使用开发者原创声明 1.1（Developer Certificate of Origin，DCO），不另外要求签署贡献者许可协议。Pull Request 中的每个提交都必须包含使用真实姓名和邮箱的签署行：

```text
Signed-off-by: Zhang San <zhangsan@example.com>
```

创建提交时可以自动添加：

```sh
git commit --signoff
```

已有的本地提交可以在推送 Pull Request 前补充签署：

```sh
git commit --amend --signoff --no-edit
```

签署即表示您接受以下 DCO 1.1 原文声明：

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
1 Letterman Drive
Suite D4700
San Francisco, CA, 94129

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```
