# 为 BSWorld 贡献

[English](../../CONTRIBUTING.md) | [简体中文](CONTRIBUTING.md)

BSWorld 欢迎缺陷修复、测试、文档、新的 Bluetooth LE 应用环境、平台接入，以及对确定性运行时和 RF 编排的改进。

BSWorld 自有代码使用 Apache License 2.0。第三方组件沿用其原许可证，相关说明见组件源码和[第三方声明](THIRD_PARTY_NOTICES.md)。

## 开始之前

- 提交新 Issue 前，先检索是否已有相同问题。
- 按[安全漏洞报告流程](SECURITY.md)私下报告漏洞。
- 大范围架构调整、新后端或公共接口变更，请先提交 Issue 讨论设计和边界。
- 小范围修复、针对性测试和文档勘误可以直接提交 Pull Request。
- 在项目的所有交流空间中遵守[社区行为准则](CODE_OF_CONDUCT.md)。

## 搭建开发环境

开发使用[本地 Linux 环境](README.md#本地-linux)。

在 Docker 中测试时，运行[已发布的镜像](../../docker/README.zh-CN.md#运行测试)并挂载检出的代码。如果修改了固件或 Dockerfile，请[从当前代码构建镜像](../../docker/README.zh-CN.md#构建)。

## 修改代码

- 每项修改聚焦一个问题或行为，避免无关重构和生成文件变更。
- 可复用的框架代码放在 `src/bsworld/`。应用专用的进程配置、构建文件和测试场景放在 `tests/`。
- 异步测试使用标准 asyncio API，等待时使用 `asyncio.sleep()` 等异步接口。
- 对外行为变化时，新增或更新测试，优先对公开结果作断言。
- 行为、命令或公共接口发生变化时，中英文文档必须同步更新。
- 提交内容应排除本地编译产物、缓存和测试日志。
- 新增自有源码、配置和脚本时，沿用相邻文件的 Apache-2.0 版权头。保留第三方代码原有的许可证声明，引入外部组件时更新 `THIRD_PARTY_NOTICES*`。

## 验证修改

在本地或 Docker 中运行与修改相关的检查。镜像的选择方法见[验证修改](development.md#验证修改)。

框架代码至少要通过静态检查、格式检查和单元测试。

```sh
ruff check --no-cache .
ruff format --check --no-cache .
pytest tests/unit
```

遵循仓库的 [Python 代码格式约定](development.md#代码格式)。发起针对 `main` 的 Pull Request 或向 `main` 推送时，CI 会运行 `Checks`，覆盖静态检查、格式检查和单元测试。

修改进程启动、调度、HCI、RF 行为、平台配置或测试应用时，运行受影响的集成测试。准备好集成环境后，从仓库根目录运行套件。

```sh
pytest tests/integration -n auto
```

修改涉及多个子系统时，运行 `tests/` 下的全部测试。

```sh
pytest tests/ -n auto
```

如果本地无法运行某项必要测试，请在 Pull Request 中明确说明未运行的内容和原因。

## 提交 Pull Request

- 说明问题和修改原因。
- 描述修改后的行为和对兼容性的影响。
- 列出实际执行的验证命令和结果。
- 根据需要同步提交测试和文档。
- 说明新增依赖、复制的代码、生成的资源及许可证变化。
- 将凭据、私有日志、机器专用路径和无关二进制文件排除在 Pull Request 之外。

较大的修改可能需要拆成多个提交，方便审查。提交信息使用简短的祈使句，描述改动及其影响。

## 开发者原创声明

贡献者按[开发者原创声明 1.1](https://developercertificate.org/)（Developer Certificate of Origin，DCO）作出授权声明。Pull Request 中的每个提交都必须包含 `Signed-off-by` 行，填写贡献者的真实姓名和邮箱。

```text
Signed-off-by: Zhang San <zhangsan@example.com>
```

创建提交时使用 `--signoff` 自动添加签署行。

```sh
git commit --signoff
```

为已有的本地提交补充签署时，在发布 Pull Request 前修改该提交。

```sh
git commit --amend --signoff --no-edit
```

签署即接受下方的 DCO 1.1 声明。

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
