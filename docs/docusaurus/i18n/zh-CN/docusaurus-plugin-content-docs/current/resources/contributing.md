---
title: 贡献指南
slug: /resources/contributing
---

# 为 Lens 贡献代码

请将改动范围限定在 [README.md](https://github.com/llm-d-incubation/llm-d-lens/blob/main/README.md)
中列出的 10 个页面及其配套工作流内。

## Issue 与 Pull Request

请使用本仓库的 issue 来报告 Bug 和提出功能需求。请包含复现步骤、预期行为与
实际行为、提交或版本号，以及相关的环境信息。提交日志前请删除凭据和隐私数据。

每个 Pull Request 都应描述问题、产生的行为以及验证方式。请说明任何已知的
失败项或未经测试的外部集成。请遵循 [.agents/skills/ui/SKILL.md](https://github.com/llm-d-incubation/llm-d-lens/blob/main/.agents/skills/ui/SKILL.md)
中共享的 UI 约定以及[行为准则](https://github.com/llm-d-incubation/llm-d-lens/blob/main/CODE_OF_CONDUCT.md)。

## 仓库语言

请使用英文撰写所有仓库内容以及 PR 标题/描述。提交或创建/更新 PR 前，请运行
`npm run check:english`。Agent 必须自动翻译检查发现的问题，审阅其含义，
并反复运行直至检查通过。使用
`-- --text-file /tmp/pr-title.txt --text-file /tmp/pr-body.md`
可将 PR 元数据也纳入检查范围。该扫描工具会检测 Git 可见文本及文件名中的
汉字字符；它并不是针对其他语言的自然语言分类器。请勿通过转义字符替换文本
或删除有用内容来绕过该策略。唯一的例外是 `docs/docusaurus/i18n/`
（双语文档站点的中文翻译目录），该扫描工具会有意跳过它。

## 开发与验证

在修改行为之前，请遵循 [AGENTS.md](https://github.com/llm-d-incubation/llm-d-lens/blob/main/AGENTS.md)
以及[复用优先工作流](https://github.com/llm-d-incubation/llm-d-lens/blob/main/docs/refactoring/reuse-first-agent-design.md)。
请查找已有实现，比较契约与调用方，并在 PR 中记录复用或新写代码的理由。
对于未解决的业务、兼容性、CI 例外或目录决策，应暂停相关依赖工作；在继续
推进前，务必获取并记录用户的实际答复。

对复用工具的改动请运行 `npm run reuse:test`；对行为的改动请运行
`npm run reuse:check -- --task <id> --base <task-start-commit> --report .cache/reuse/report.json`。
请遵循该工作流中的 `begin`、`search --task` 和 `verify` 证据步骤；交付前运行
`verify-report --report .cache/reuse/report.json`。请导出已忽略的任务证据
和命令日志以供审阅。报告的有效性仅限于 Git 可见的输入；被忽略的运行时数据
不在其有效性保证范围内。当能力条目发生变化时，请更新 `.reuse/catalog.json`
并运行 `npm run reuse:map`。相似性候选项需要人工分析，而不能自动重构。

请参考 [README.md](https://github.com/llm-d-incubation/llm-d-lens/blob/main/README.md)
完成环境搭建。根据改动内容选择合适的检查命令：

```bash
npm run build
npm run type-check
make test-js
make test-python
make lint
```

请在 `specs/changes/` 下记录较大的功能设计文档。保持 API 描述及相关文档与
前后端行为同步。真实集群部署和外部服务提供方调用需要配置好的环境；请单独
说明这些流程是否已经过实际验证。

请保留上游的 Apache 2.0 [许可证](https://github.com/llm-d-incubation/llm-d-lens/blob/main/LICENSE)
及源代码版权声明。
