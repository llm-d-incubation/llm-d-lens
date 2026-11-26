---
title: 贡献指南
slug: /resources/contributing
---

## 治理结构

`llm-d-lens` 采用如下层级化的技术治理结构：

- **贡献者（contributors）** 社区：负责提交 issue 和 pull request。
- **核心维护者（core maintainers）** 团队：整体负责 `llm-d-lens` 并推动其演进。

所有贡献均应遵循核心维护者执行的 `llm-d-lens` 设计原则与最佳实践。我们非常欢迎并鼓励高质量的 pull request，但所有维护者均有权自行决定优先处理各自的工作，因此贡献者不应预期其提交会立即得到评审。

在向维护者发送 PR 之前达到高质量标准，可以最大限度地提高贡献被接纳的机会。

### 核心维护者

核心维护者主导 `llm-d-lens` 的开发，并为更广泛的 `llm-d` 项目定义基准测试基础设施与策略。其职责包括：

- 为 `llm-d` 提议、实现并评审负载画像、参数配置、运行规则、数据采集及工作负载分析
- 执行代码质量标准并确保遵循核心设计原则

核心维护者应公开阐述其决策过程，并分享决策、否决及争议解决背后的理由。

核心维护者名单见 [OWNERS](https://github.com/llm-d-incubation/llm-d-lens/blob/main/OWNERS) 文件。

### 决策机制

#### 无争议变更

我们致力于接纳符合质量标准的常规功能性 Bug 修复——前提是包含精简的单元测试以避免未来回归。性能改进通常也属于此类，但若核心维护者认为其收益与复杂度之间的权衡不够理想，则可能被拒绝。既不修复已知功能问题也不改善性能的设计变更，将被自动视为存在争议的变更。

#### 有争议变更

更具争议性的设计变更（例如对工作负载画像、负载生成器、运行规则或数据采集与分析工具的破坏性变更）将由核心维护者根据具体情况进行主观评估。

## Issue 与 Pull Request

请使用本仓库的 issue 来报告 Bug 和提出功能需求。请包含复现步骤、预期行为与
实际行为、提交或版本号，以及相关的环境信息。提交日志前请删除凭据和隐私数据。

每个 Pull Request 都应描述问题、产生的行为以及验证方式。请说明任何已知的
失败项或未经测试的外部集成。请遵循 [.agents/skills/ui/SKILL.md](https://github.com/llm-d-incubation/llm-d-lens/blob/main/.agents/skills/ui/SKILL.md)
中共享的 UI 约定以及[行为准则](https://github.com/llm-d-incubation/llm-d-lens/blob/main/CODE_OF_CONDUCT.md)。

提交 Pull Request 前，请确保完成以下步骤：

1. [为你的提交签名](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits)
2. 在推送 PR 内容**之前**，确保已运行 [pre-commit](https://pre-commit.com/) 钩子并通过检查。参见下方的[开发与验证](#开发与验证)。

## 发布在线安装程序

在 `main` 分支中保持 `LENS_SOURCE_VERSION="main"`。发布版本时，请从目标提交创建发布分支，并在打 tag 前为在线安装程序写入版本标记：

```bash
bash scripts/LensInstaller-Ubuntu-x86_64-online.sh release stamp vX.Y.Z
bash scripts/LensInstaller-Ubuntu-x86_64-online.sh release check vX.Y.Z
```

将 `vX.Y.Z` 替换为实际的 tag。提交已打标脚本并打上该 tag，待该 tag 上的 **Installer checks** 通过后再公布原始下载 URL。保持发布 tag 不可变。不要将已打标的版本合回 `main`；`main` 与 PR 检查要求版本为 `main`，而 tag 检查要求与 tag 名称完全一致。该工作流仅校验 tag，不会创建 GitHub Release 或强制执行 tag 保护。不支持为没有在线安装入口的旧 tag 追溯打标。

唯一的公开安装入口是 `scripts/LensInstaller-Ubuntu-x86_64-online.sh`。同一脚本内包含下载阶段、安装引擎和 Shell 发布版本命令。下载源码归档后，它会以内部源码安装模式调用归档中的副本，而不会递归下载。CI 会在 Ubuntu 22.04 及其系统 Python 3.10 环境下检查 Shell 语法/lint、版本一致性、独立帮助信息、私有 Python 环境准备以及宿主机 OS 层命令行（`check`、`info`、`packages missing`），同时使用 `bash -n` 和 `shellcheck` 检查生成的 `lensctl.sh`，并在宿主机 OS 层源码变更时触发运行。

## 仓库语言

请使用英文撰写所有仓库内容以及 PR 标题/描述。提交或创建/更新 PR 前，请运行
`npm run check:english`。Agent 必须自动翻译检查发现的问题，审阅其含义，
并反复运行直至检查通过。使用
`-- --text-file /tmp/pr-title.txt --text-file /tmp/pr-body.md`
可将 PR 元数据也纳入检查范围。该扫描工具会检测 Git 可见文本及文件名中的
汉字字符；它并不是针对其他语言的自然语言分类器。请勿通过转义字符替换文本
或删除有用内容来绕过该策略。唯一的例外是 `docs/site/i18n/zh-CN/`
（双语文档站点的中文翻译目录），该扫描工具会有意跳过它。
`docs/site/i18n/en/` 下的英文正文仍完整参与扫描。

## 开发与验证

### 定期完整本地验证

`npm run verify:full`（`make verify-full`）是一个手动检查点，用于在不依赖缓存通过结果的情况下，运行仓库范围的构建、lint、英文检查、JavaScript/Python/工具测试、前端构建以及双语文档构建。请先安装工作区及 Python 开发/嵌入式数据库依赖；如果 Python 不在 `.venv/bin/python`，请设置 `PRISM_PYTHON_BIN`（回退为 `python3`）。每项检查都会打印 `PASS`/`FAIL`，并将完整日志保存在打印出的 `.cache/reuse/full-validation/` 下的唯一目录中。某项失败不会阻止后续检查，任何失败或缺少工具都会使命令以非零状态退出。测试数据相互隔离。这是一个简单的 Makefile 入口，而非证据/报告框架。运行期间请勿编辑源码；在相关源码或环境变更后重新运行。日常修复请使用定向测试；Agent 仅读取失败日志。真实集群/提供方验收、容器、在线栈检查、模型冒烟测试以及下方的安全要求均独立进行；测试跳过项和构建警告会保留在日志中。

可选的仓库钩子（`git config core.hooksPath hack/hooks`）会检查暂存区的空白字符和英文文本，而不运行回归测试套件。pre-commit 框架配置仍可用于暂存区文件卫生与 linter 检查。完整回归检查属于 CI；请勿在每次提交前同时运行完整套件和子集套件。

前端源码变更请使用 Vite 热重载。`hack/lens-local.sh restart backend` 或 `restart server` 仅重启所请求的服务。端口/环境变更以及会改变服务端 MCP 目录的 API 变更请使用 `restart all`。实际服务端口会记录下来以供定向重启使用。对于使用旧版启动器启动的会话，请运行一次 `restart all` 以初始化该状态。

失败的 Deploy Kubernetes 命令会在后端日志中输出 `deployment_kubernetes_command_failed`。该结构化记录会标识传输方式、命名空间、CLI PID、所选 API 来源、kubeconfig/缓存路径、脱敏后的代理来源，以及每个 `NO_PROXY` 变体中的字面 API 主机条目。此诊断不会评估 CIDR/域名绕过规则。凭据、完整命令参数和原始 kubeconfig 内容不会被记录；命令结果与重试行为保持不变。

当在同一进程中运行迁移命令时，Alembic 日志配置会保持现有应用 logger 处于启用状态。

在修改行为之前，请遵循 [AGENTS.md](https://github.com/llm-d-incubation/llm-d-lens/blob/main/AGENTS.md)
以及[复用优先工作流](https://github.com/llm-d-incubation/llm-d-lens/blob/main/docs/design/reuse-workflow.md)。
请查找已有实现，比较契约与调用方，并在 PR 中记录复用或新写代码的理由。
对于未解决的业务、兼容性、CI 例外或目录决策，应暂停相关依赖工作；在继续
推进前，务必获取并记录用户的实际答复。

对复用工具的改动请运行 `npm run reuse:test`，其他改动直接运行受影响的测试。
不再要求任务台账、自动审计报告或测试结果指纹。说明实际检查结果和未验证范围；
相关代码或环境变化后重新验证。阶段性完整本地验证使用 `npm run verify:full`。
当能力条目发生变化时，更新 `.reuse/catalog.json` 并运行 `npm run reuse:map`。
相似性候选需要分析，不能据此自动重构。已有未决事项仍需处理，不能视为已批准。

请参考 [README.md](https://github.com/llm-d-incubation/llm-d-lens/blob/main/README.md)
完成环境搭建。根据改动内容选择合适的检查命令：

```bash
npm run build
npm run type-check
make test-js
make test-python
make lint
```

请在 `docs/design/` 下记录较大的技术设计：每个产品模块或共享领域只保留一份
规范设计文档，文件名使用小写英文和中划线，不带语言后缀。合并相关设计时，
只保留有效的设计、约束和明确的实现边界，不保留已被替代的历史方案或实施快照。
重命名、合并或删除文档后，同步更新链接、锚点、代码注释、规则、忽略列表以及
文档同步和 CI 路径，并保持已发布页面的地址不变。

在 `docs/site/i18n/en/docusaurus-plugin-content-docs/current/` 下维护用户指南和 API 参考。保持 API 描述及相关文档与
前后端行为同步。真实集群部署和外部服务提供方调用需要配置好的环境；请单独
说明这些流程是否已经过实际验证。

## 安全验证要求

范围包括应用代码、自动化脚本、依赖清单与锁文件、容器、随产品交付的已安装依赖
和发布产物。PR 与依赖更新 PR 都是不可信输入，应使用隔离 Runner，不能暴露部署凭据
或长期密钥。Runner、扫描器及其规则/签名更新渠道属于可信基础设施；结果仅适用于
被扫描内容，不能证明运行环境安全。

**仓库当前配置：** `.github/workflows/ci-pr-checks.yaml` 面向以 `main` 为目标的 PR，
检查英文内容；检测到代码变化时，还检查固定的 llm-d 版本组合、前端构建、
MCP 生成、TypeScript、JS 测试、Ruff/格式和容器构建，但不运行 `npm run lint`。
仓库未配置 CodeQL、REUSE 许可证或恶意软件扫描 Job。
`.github/dependabot.yml` 只有每周的 GitHub Actions 和 Docker 更新，没有 npm/pip。
托管 CodeQL 设置、分支保护、必需检查、Dependabot 告警与安全更新需另行在 GitHub
设置中核实。

以下是**要求达到的验证策略**，不表示所有检查已经接入 CI。
启用缺失检查或批准例外需要维护者另行决定；本次文档迁移不执行这些操作。

| ID | 验收要求 |
| --- | --- |
| CQ-1 | 代码 PR 的现有构建、类型检查、测试、Ruff/格式和容器检查通过，并将 `npm run lint` 设为必需检查。失败不得合并；例外需负责人、理由和到期日。纯文档 PR 可跳过构建/测试，但英文和许可证检查仍适用。 |
| SAST-1 | 代码 PR 完成官方 JavaScript/TypeScript、Python CodeQL 安全查询并发布 SARIF；分析失败阻止合并。逐项分诊，高危/严重问题在没有已批准限时例外时阻止合并。保持查询和告警最新；本地分诊不能代替 CI。 |
| REUSE-1 | 每个 PR（包括纯文档）运行官方 `reuse lint`。版本控制文件需有效的 SPDX 许可证/版权元数据，或符合标准的许可证文件/注解；失败阻止合并。 |
| MAL-1 | 每个 PR 和发布前，用 ClamAV 或等效扫描器检查仓库、随产品交付的已安装依赖和实际交付产物。更新签名；检出问题、扫描/更新失败、签名过期或产物漏扫都阻止验收/发布。误报按文件/哈希记录理由和处理结论，不能全局排除。 |
| DEP-1 | 保留每周 Actions/Docker 更新，并补齐 npm/pip。启用并核实 Dependabot 告警/安全更新；依赖 PR 通过 CQ-1、REUSE-1、MAL-1。优先处理高危/严重告警；无法升级时记录缓解措施、负责人和复查日期。 |

**REUSE-1 指 REUSE.software 许可证合规**，不是 Lens 的代码复用目录或重复检测。
`npm run reuse:*` 不能替代 `reuse lint`。

维护者需确认必需状态检查实际执行 CQ-1、SAST-1、REUSE-1、MAL-1，并确认发布扫描
覆盖实际产物而非仅源码。修改扫描配置后，使用隔离的标准可检测测试样本验证阻断行为；
不得将样本提交到仓库或加入发布产物。至少每季度检查告警积压、扫描器/规则更新、
例外有效期以及 GitHub 必需检查设置。

当前扫描证据与处理结论应关联上述五个目标 ID。若采用仓库威胁模型或 SDL 任务清单，
再将其具体 ID 映射到这些用例。这些要求本身不证明覆盖了更广泛的威胁；
旧的未分诊扫描数量不是已确认漏洞，也不是当前通过结论。

请保留上游的 Apache 2.0 [许可证](https://github.com/llm-d-incubation/llm-d-lens/blob/main/LICENSE)
及源代码版权声明。
