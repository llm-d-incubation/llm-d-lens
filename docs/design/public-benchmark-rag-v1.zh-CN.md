# 公开基准测试 RAG v1（中文译本）

> 本文首节是当前存储架构及 D1 状态，和英文方案的已批准架构保持一致；下方 S1、截至 2026-10-20 的实施/部署记录及旧方案均为历史材料，不代表当前职责或部署状态。路径、技术标识及历史测试数据按记录保留。当前英文方案见[公开基准测试 RAG v1](./public-benchmark-rag-v1.md)。

## 当前架构与 D1 状态（2026-10-27）

### 存储职责

- **公开文档仅由 OpenSearch 提供权威存储与检索。** `public-rag-management-v1` 管理文档来源、审批、发布清单、构建历史和重建请求；检索 alias 是活动构建的唯一依据，每次请求固定一个物理构建及其模型合同。PostgreSQL 不再是公开文档的权威来源，也不是检索失败时的回退路径。
- **原始来源字节继续保存在获批的原文归档中。** OpenSearch 和 embedding 缓存都不能替代原始字节归档。
- SQL `public_benchmark_records` 专用于 Prism 结构化实验记录及其必需的来源/发布关联，不用于 OpenSearch 文档 RAG。获批清理会创建空表；不会复制旧 RAG 行或导入 Prism 快照。内部实验存储不变。
- SQL 旧 RAG 路径涉及的三张表是 `rag_items`、`rag_search_builds`、`rag_search_pointers`。清理后它们退役，不作为运行时回退存储；旧 SQL 文档全文检索及搜索协调路径不再承担公开文档职责。`LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY` 已移除；遗留 `postgres` 配置会被拒绝。
- 混合文档发布清单可保留原始 manifest 哈希、成员身份和引用 ID。OpenSearch 只保存文档检索所需的外部身份/依赖描述，不保存结构化实验、测量、图片或链接事实正文。

### 配置线索与统计默认值（2026-10-28 更新）

查询筛选、规划比较和模型上下文将 `statistic: null` 默认解释为 `mean`；
显式 P95/P99 等统计口径保持独立。存储记录及查询证据保留原始 null，
数值回答明确标注默认值；模型测量及比较注解通过 `statistic_original=null`
和 `statistic_basis="default_mean"` 标记该解释，不改写历史测量或声称来源明确报告了 mean。
此规则取代历史材料中“缺失统计量不默认 mean”的要求。

创建与 refine 的模型上下文保留 `run_id`、`source_path` 和
`source_info.file_identifier`，包括 scorer、prefix-cache 等配置线索。
它们是待核验的来源提示，不等于完整 serving 配置或候选性能保证；
冗长导入元数据及原始 URL 仍仅保留在审计记录中，投影继续受既有证据预算约束。

模型侧 `configuration.technique_hints` 从上述三个来源字段显式提取已识别的技术标签。
每条包含 `provider_ref`、`technique`、`source_field`、`source_value` 和
`verification="unverified_source_hint"`。provider 仅表示可能相关的当前技术，
不是已核验的历史 provider/guide；不从泛化 prefix/cache 标签、导入元数据或原始 URL 推断。
两个 AI 阶段共用该投影，不改写历史配置。排序 prompt 要求解释相关已提供候选的采纳或排除理由，
即使该候选未进入返回的前三名，并引用用于比较的案例或测量；不得将特定技术的测量改称为通用
baseline 性能。这是 prompt 约束，不保证模型必然遵守，也不新增确定性排名规则。

入库合同不等于配置完整性门槛：`public_benchmark_records` 同时保存来源/发布关联、
实验和测量。experiment 只强制 `source_pointer`，允许 configuration 为空；
Prism 导入器还会将非指标记录元数据放入 configuration，因此非空对象也可能没有 serving 参数。
此类记录只能带明确未知项作为历史参考，不能称为完整可复现部署。

### 配置决策文档相关性（2026-10-28 更新）

创建与 refine 的初查携带配置、适用条件、前提、限制、负载与 SLO 意图。
候选生成校验后，每个不同的可部署 provider 技术各补查一次（最多五次），
共享独立 10 秒预算，不继承已被生成过程消耗的初查截止时间；
初查仍有 60 秒上限，补查超时保留成功分支并明确记录。
每次查询先对最多二十个候选段落进行相关性重排，再保留五段；最终共享包仍最多二十段。
正文中的方法配置/限制线索及新增技术覆盖度驱动启发式排序；路径、标题仅作辅助，
通用发布流程或 API 支持不能充当具体配置依据。两个模型上下文均标注其启发式性质，
要求引用实际支持结论的正文，否则不引用。
预算先保留最优完整测量案例，再预留不同方法的配置指导；实际请求优先裁减尾部文档，
不改变 SQL 案例排序，不截断段落或去掉限制条件。诊断记录相关性、排序/保留 ID、
补查技术、结果、超时与时间预算。
模型示例和已注册硬件 profile 线索进一步细化相关性；未知硬件保持中性，同 profile
不等于精确设备兼容。首段选出后，优先保留尚未覆盖方法的配置指导，再保留重复上下文，
其他模型的示例可作为带限定条件的参考。来源成员读取改为请求内 `_mget` 批量校验，
每批最多 256 条，保留完整校验与返回前 head 复查。Hybrid 不再将候选池静默限制为十条；
冷模型就绪使用独立 8 秒上限，查询 embedding/每页向量检索仍有 5 秒上限。

### SQL-only 清理与运维边界

- `benchmark_rag/cutover.py` 只操作 PostgreSQL：先将三张旧表及其行写入带校验和的 `legacy-sql.zip` 备份，再退役旧表并创建空的 Prism `public_benchmark_records`。托管数据库使用迁移修订 `8bf2a6c1d903`；无 Alembic 历史的数据库使用同一受保护事务，不伪造迁移历史。
- 执行前，操作方必须停止或静默**所有旧版及文档写入者**，并在整个清理期间保持静默。`--writers-quiesced` 只是操作者的显式确认，命令不会代为停止服务。备份目录必须持久且备份验证成功。
- 清理命令不连接 OpenSearch、不切换 alias、不构建索引，也不删除原始来源快照。成功生成的 `committed.json` 只证明 SQL 清理已提交，**不证明文档已迁移、检索可用或 S3 质量达标**。已有 Prism 行或目标表已存在时，清理会拒绝执行。
- 文档管理的 `backup`、`verify`、`restore` 默认使用 OpenSearch 文档包；结构化 SQL 传输须显式选择 `--structured`。恢复不会授予来源审批或激活 alias；必须保留撤销状态，并在当前审批、发布及检索重建验证完成后才能提供检索。
- `refresh`、`status` 和来源 `rollback` 对 Prism 走 SQL，对其他公开文档来源走文档仓库。现有 `rebuild` 命令仍只负责 Prism SQL 导入；OpenSearch 检索构建使用 `PublicRagIndexBuilder`。
- 文档元数据和 alias 写入由持久化 `flock` 协调，当前只支持共享持久数据目录的**单主机**写入，不支持多主机协调。alias 操作结果不确定时失败关闭；显式对账前必须排空在途请求，查询或恢复不会静默修复 alias。来源 head 回滚不等同于检索 alias 恢复；不存在仅靠切换配置即可回滚 PostgreSQL 的方案。

### D1 完成状态

OpenSearch-only 文档职责及 SQL-only 清理方案已写入代码和运维设计；清理实现会先生成并校验 SQL 备份。真实环境 SQL cutover 尚未执行。

2026-10-27 在本机 OpenSearch 3.3.2 上通过了 D1 生命周期定向集成测试（1 passed）：使用临时合成来源和临时原文归档，真实执行发布、建索引与检索，核对唯一 alias、旧 worker fencing、并发激活、alias 已切换但响应丢失后的显式恢复、引用哈希/路径/原文，以及撤销后不再返回结果。恢复测试注入的是实际 alias 修改后的响应丢失，不是强制终止工作进程。测试使用随机 alias/管理索引并清理临时索引；既有活动 alias 未改变。此前 317 项生命周期、调用方及兼容定向测试通过。

此结果**不等于现有语料归档的恢复/重建验收**：当前没有已审核的八来源备份包，因此未验证完整真实语料的原文归档恢复及重建。原 v1 固定质量集仍是仅覆盖两个 GitHub 仓库的历史基线；新增 v2 草案现覆盖全部 8 个活动来源 scope，并支持逐源指标，但尚未针对已验证的完整语料包运行，也未经独立人工复核。S3 相关性、质量及逐源验收仍未完成。**因此 D1 尚不能宣告完成。**旧表清理成功本身也不构成 D1 或 S3 验收。

v2 固定集共 100 条：每个 scope 10 条正向查询（五类各 2 条，development/holdout 各一条），另有 10 条跨源消歧查询和 10 条覆盖全部 scope 的无证据查询。v1 文件保持不变以供历史对比。标注稀疏，来源覆盖本身不等于质量通过。

The quality runner searches all eight pinned scopes for every query; target
origins are attribution, not implicit retrieval filters. It scores deduplicated
global Top-10 parent evidence without promoting source-local ranks. Per-source
query averages retain those global scores, with target-source seed hits reported
separately. Reports include category/split intersections, source macro averages
per split, and micro recall from total labeled hits divided by total relevant
labels. Stale or degraded responses fail evaluation. The integration quality
checks apply the unchanged relevance thresholds to holdout as well as overall
results; these numerical checks do not replace independent annotation review.

The 2026-10-27 live, read-only 300-request static run finished without changing
aliases or labels. Hybrid overall Recall@10/nDCG@10 were 0.8000/0.6571 and holdout
were 0.8333/0.6917: both fail the 0.90/0.80 thresholds. Human sign-off remains
0/100, 1,989 pooled question/parent pairs remain unreviewed, and the 17 freeze
blockers remain. This is sparse-label diagnosis, not S3 acceptance; details are
in the [implementation plan](../rag-implementation-plan.md).

## 历史检查点：S1 公开全文检索后端（2026-10-26，旧存储职责）

`OpenSearchEvidenceSearchRepository` 及 projection writer 已迁移至 `benchmark_rag/`，
仅服务公开文档检索，不再承担诊断适配器职责；诊断制品校验仍独立保留。
新增只读 REST/MCP 接口 `POST /api/v1/benchmark-rag/search`，沿用 GLOBAL 公开查询权限。
查询固定物理索引，按来源 manifest 过滤，并回查 PostgreSQL 成员关系与归档原文后返回引用。
普通章节核对字符偏移；表格、叙述和自定义 JSON 上下文必须与原审定解析器输出及来源指针一致。
原数值查询接口及内部查询语义不变。

构建器完整验证发布语料，分批写入并核对数量和来源 generation，最后原子切换公开 alias；
S1 保留完整段落，不实现向量、混合排序、分布式激活协调、持久构建任务或配置 UI。
来源过期时明确标记并排除，OpenSearch 故障显式报错，不自动回退 PostgreSQL。
S3 协调机制完成前，运维构建必须串行执行。

本地开发索引包含两个审定仓库的 1,860 条 passage。初次 164 项定向测试之后，
固定语料、鉴权与评测验收 138 项通过，既有数值、适配器、构建与诊断回归 74 项通过，
最后新增的 10k 段落性能测试 1 项通过；这些不是全仓测试。

固定种子集共 60 条、六类各 10 条，开发/留出为 36/24，原文核对锚点共 10 个。
这是代理整理的稀疏标注，不是独立人工评审；用户接受为 S0 首集，并将独立人工复核和
扩充标注列为 S3 上线前门槛。全文有证据查询 Recall@10 为 0.78、nDCG@10 为 0.5831，
跨语言 Recall@10 仅 0.20；10 条无证据查询正确返回空结果。不代表混合上线质量已达标。

有界主键权威回查、应用生命周期连接池与惰性回查批次保留当前 manifest/原文验证。
隔离 PostgreSQL、专用 OpenSearch 3.3.2 在共享主机上运行合成 10,000 段落、
并发 10，每种边界预热 20 次、计时 500 次。服务 P95 为 1.3997 秒，
含真实 session/CSRF/路由/序列化的 ASGI HTTP P95 为 1.6225 秒；后者不含网络/TLS，
合成数据只用于负载。OpenSearch 限 2 CPU、2 GiB 内存、512 MiB heap。

真实 HTTPS 登录/REST 与 MCP initialize/list/call 均返回核验引用；匿名 401、缺 CSRF 403。
临时最小权限账号、角色和 session、压测容器与预览进程已清理；开发索引保留。
5,146 行初始备份已在临时 PostgreSQL 恢复并幂等重放。
用户最新要求明确延期前端及浏览器验收；S0–S1 后端完成不代表 UI 或生产上线完成。

S2 选定 `intfloat/multilingual-e5-small`，revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`，384 维、512-token 总预算，
使用 `query: ` / `passage: ` 前缀、attention-mask mean pooling、L2 归一化及 cosine。
MIT 来自模型卡声明，独立 LICENSE URL 返回 404，下载前仍须核对完整许可材料。
本轮未下载权重/新增 embedding 依赖，尚无向量；下一步是 CPU 有界编码、
token-aware 可溯源子块、版本化缓存和独立向量影子索引，之后才接入 S3 混合检索。
资源管理 UI 与激活协调恢复仍在后续阶段。
下方 2026-10-20 内容仅为历史检查点，不能作为当前数据库或部署状态证明。

## 历史实施进度（截至 2026-10-20）

**当前位置：第 1–8 步及第 3a 步工具已完成开发、本机验证并提交；`agentic_deploy_v2` 已先推送至 `c32dec1`，再推送至最新修复 `9bb27e5`。第 3a 步真实目标操作已开始，但停在 schema 评审，未完成目标恢复或部署验收；不能宣称 3a 全部完成。6a 继续跳过，3b 尚未实施。内部查询仍须显式 opt-in 与所属上下文授权。**

> 本节最新状态优先于下方历史记录中的“未推送”“尚未开始”等表述；历史检查点保留，不代表当前分支状态。中英文规划文档继续被 Git 忽略，仅本地更新，不提交或推送；用户提供的只读附件未修改。

### 最新代码与验证（`9bb27e5` 后）

- `9ae2183` 保存 review 修复检查点；`c32dec1` 完成查询解析边界修复与最终验证，详见[执行记录](../benchmark-rag-review-2026-10-20.md)。
- 本轮移植审核重新运行 **83 项迁移/表结构测试**及 **4 项公开基线/仓库移植测试**，全部通过、无跳过；后者包括真实三源固定公开基线 **20/20**，以及固定指南/文档语料的备份恢复。
- 当前代码成功校验既有真实三源逻辑包：**14,961 条保留行、190 个原始证据 blob**。在全新临时 PostgreSQL 和独立证据目录中，首次恢复 **14,961 行、更新 3 个来源**；重复恢复 **插入 0 行、更新来源 0 个**。计数包含历史与清单，不代表独立实验数。
- `9bb27e5` 在归档读取层修复损坏 manifest：无效编码、所属者、条目结构和来源解析错误转换为 `PublicationError`，由查询服务记录日志并转换为 `EvidenceUnavailableError`。REST 返回 **HTTP 503**、错误码 `rag_evidence_unavailable`，不返回证据或部分答案。
- **13 个新增损坏用例在修复前全部失败**；最终查询、REST、迁移与发布回归 **159 项通过，无跳过**。每个损坏用例均验证归档/查询错误、实际 HTTP 503，以及恢复原清单后 HTTP 200 且答案保持原样。Ruff lint/format、修改后的归档读取器 Pylance 诊断、IDE 问题、空白、英文和文档链接检查通过；中英文文档构建通过。既有弃用、OWNERS 链接及缺失 blog 警告保留，不声称完整工作区类型检查通过。
- 本次代码、回归及公开中英文 API 文档已随 `9bb27e5` 提交并推送；审核、修复及本机验证没有操作真实目标或业务数据库。

### 截至 2026-10-20 记录的第 3a 步待验收项（历史）

当时记录的真实目标停点（见下方“历史停点”章节）为：版本 `d9e3f7b1c5a8`、没有 `rag_items`，已有业务字段 `audit_logs.cluster_id` 数据库为 `VARCHAR(8)`、模型为 `VARCHAR(32)`。**该状态未在本次更新中重新远程核查，不代表当前目标状态**，也不构成修改业务字段或绕过托管迁移的批准。

后续须重新预检目标、批准并执行兼容的 RAG-only 原生迁移，集成当前后端/Node 代码，对齐运行数据库/search_path、持久化证据目录与真实调用者授权，再恢复完整逻辑包。核验当前/历史计数、数值样本、哈希、重复恢复、REST/MCP、重启持久化和来源不可用时目标独立运行后，才能宣布 3a 完成。CLI `rebuild` 仍仅处理初始 Prism；完整三源采用 `backup → verify → restore`。本机结果不替代真实目标验收。

- 工作仓库：[llm-d-lens](../../)。
- 当前分支：`agentic_deploy_v2`，从更新后的 `main`（`8f6fc9e`）创建。
- 第 1 轮提交：`6066101`，提交信息为 `feat(rag): add versioned public benchmark storage contract`。
- 第 2 轮提交：`cf7422e`，提交信息为 `feat(rag): add atomic public benchmark publication`。
- 第 3 轮提交：`1c43ec9`，提交信息为 `feat(rag): import public benchmark corpora and add portable administration`，包括已 review 的第 3、3a、4 步代码与操作说明。
- 第 4 轮提交：`86ef3f6`，提交信息为 `feat(rag): add authorized public retrieval via REST and MCP`，包括已 review 的第 5 步代码、测试及公开操作文档。
- 第 5 轮提交：`3ab782d`，提交信息为 `feat(rag): add opt-in caller-authorized Agentic retrieval`，包含已 review 的第 6 步代码、测试与公开文档。
- 第 6 轮提交：`c5ee9a1`，提交信息为 `feat(rag): add reviewed refresh jobs and fixed evidence evaluation`，包含已 review 的第 7/8 步。
- 第 7 轮提交：`c6f4796`，提交信息为 `fix(rag): preserve original evidence whitespace in responses`，包含真实验收暴露的引用空白修复、回归与公开中英说明。
- 后续提交：`9ae2183`、`c32dec1` 为 review 修复与最终验证；`9bb27e5` 为归档清单错误处理及 13 项回归。
- 提交状态：以上代码提交均已随当前分支推送。仅本地规划文档继续不提交、不推送。第 6a 步保留计划但按用户指令跳过，不修改入库逻辑。
- Review 状态：第 1–8 步及第 3a 步工具实现、本轮持久化本机实例验收与引用空白修复均已通过；内部查询默认关闭。真实跨集群和共享部署仍未验收。
- 验证范围：历史单测使用隔离 PostgreSQL 和临时证据目录；本轮新增独立本机持久化数据库及在线 REST/MCP 实例，未连接既有业务库或远程机器。单测 schema 已删除、临时测试实例已停止；新持久化验收实例刻意保留运行，与 pytest 临时数据不同。
- 本文已移至仓库 `docs/design/`，作为仅本地保留的中文规划与进度文档；该路径已被现有 Git 忽略规则覆盖，不纳入提交或推送。此前已按用户要求在[英文方案](./public-benchmark-rag-v1.md)补充两种部署场景及第 3a/3b 步。下方原方案译文仅调整迁移所需的相对链接，新规划以上方进度章节及英文方案新增内容为准；用户提供的只读附件未修改。

### 各步骤状态

| 步骤 | 当前状态 | 说明 |
| --- | --- | --- |
| 第 0 步：固定输入与访问边界 | 原文已有验收记录 | 保留原文 2026-10-14 的固定输入、权限和引用策略；本轮重新校验 Prism 22 份输入的路径/字节哈希，未联网抓取，也未扩大来源范围。 |
| 第 1 步：单表契约与迁移 | 已完成，review 通过，已本地提交 | 模型、DTO、DAO、Alembic 迁移、测试和相关数据库说明已落地。迁移只在隔离测试数据库中执行，未应用到业务数据库。 |
| 第 2 步：首次导入前的发布机制 | 已完成，review 通过，已本地提交 | 来源注册、完整清单校验、不可变原始字节归档、暂存、原子切换、删除标记、回滚和同源并发控制；没有新增 schema 或迁移。 |
| 第 3 步：初始 Prism 快照导入 | 已完成，隔离验收与 review 通过，已本地提交 | 真实快照发布 249 条实验、9,060 条测量和 4 条摘要上下文；重复导入不增行，不改变指针或 generation。业务库未导入。 |
| 第 3a 步：跨集群 RAG 迁移 | 工具开发/本机验证完成并已推送；真实目标停在 schema 评审，验收未完成 | 本地命令、完整三源包恢复及重复恢复通过；待目标原生迁移、数据恢复与 REST/MCP/持久化/独立运行验收。不含全库迁移、SSH 编排或持续复制。 |
| 第 3b 步：同集群共享部署 | 仅规划，未开始 | 多实例连接同一 database/schema，共享证据存储；待配置、权限/连接池、管理 Job、多 Pod 与跨节点验收。用户检索验收依赖第 5 步。 |
| 第 4 步：指南结果与文档导入 | 已完成，隔离验收与 review 通过，已本地提交 | 固定提交的 139 份指南输入与 28 份基准文档；445 条来源范围实验、2,153 条测量、1,860 条段落、37 张图及 480 条图关联。第 4 步数据库回归 281 项通过，无跳过。 |
| 第 5 步：仅公开来源检索与回答 | 已完成，隔离验收与 review 通过，已本地提交 | 同一调用者范围服务提供 REST 与只读 MCP；精确数值/条件过滤、全文段落、固定证据引用、拒答、发布撤销检查和专用全局权限。默认请求行为继续保持；未部署到真实集群。 |
| 第 6 步：Agentic 授权检索分支 | 已完成，隔离验收与 review 通过，已本地提交 | 仅显式 opt-in；可验证的 Evaluate 归属及调用者逐条授权先于计数/分页；双来源证明合并与不确定重叠分离。不接入规划器。 |
| 第 6a 步：Agentic benchmark / simulation 入库完善 | 用户要求跳过，计划保留 | 不作为第 7/8 步前置，不修改 `agentic_benchmark_records`、`simulation_tasks` 写入，也不放宽现有内部检索归属边界。 |
| 第 7 步：刷新场景与触发机制 | 已实现，review 通过，已本地提交 | 手动 Prism、commit/periodic 仓库共用本地 CLI；完整审核计划、状态/计数报告、generation 保护与显式回滚。309 项受影响回归通过，无跳过；本轮另有持久化实例真实刷新/回滚验收，未安装定时任务或集群 Job。 |
| 第 8 步：固定问答基线与额外来源门槛 | 已实现，review 通过，已本地提交 | 隔离公开 20/20、内部 12/12；最终 70 项回归通过。本轮实际 HTTP 另过公开 20/20，并核验原始空白/坐标一致性；额外来源仍须单独审批。 |

### 第 1 步已交付内容与验证

1. [存储契约](../../apps/backend/src/llm_d_lens/benchmark_rag/contracts.py)：六类条目、逐类型字段校验、原始值/单位保留、显式单位与统计量映射，以及未知值保留规则。
2. [单表模型](../../apps/backend/src/llm_d_lens/db/models/rag_item.py)：`rag_items`、来源范围版本唯一键、自引用外键、CHECK 约束、类型化数值字段、JSONB，以及 PostgreSQL 生成的 TSVECTOR/GIN 索引。
3. [导入侧 DAO](../../apps/backend/src/llm_d_lens/db/dao/rag_item.py)：不可变版本写入、同版本与并发重试幂等、冲突显式报错、链接目标类型和来源/版本校验；不提供更新/删除操作。内部审计读取不是面向用户的授权检索。
4. [Alembic 迁移](../../apps/backend/src/llm_d_lens/db/migrations/versions/7a12c8e49b60_create_rag_items.py)：修订 `7a12c8e49b60` 接在 `42a3e9b47726` 之后，只新增 RAG 表及相关约束/索引。
5. [数据库测试](../../apps/backend/tests/unit/db/test_rag_item_dao.py) 和[数据库说明](../../apps/backend/src/llm_d_lens/db/README.md)：最后一次数据库层回归为 **98 项通过**，包括隔离 PostgreSQL 16.2 上的增量迁移 upgrade/downgrade、精确数值过滤、全文检索与 GIN、非法行拒绝、并发幂等、引用删除保护，以及 SQLite 单测兼容性。此结果不代表全历史迁移回放或端到端问答验收。
6. 本轮 Ruff、文档链接检查和 Git 差异空白检查通过；提交前仓库英文检查通过。

**第 1 步边界：**当时仅交付存储基础；第 2 步的实际实现与验证见下节。两轮均未修改业务数据库、导入真实数据、写入 Agentic 基准测试记录、改变规划排序或 SLO 决策，或开放查询端点。

### 第 2 步已交付内容与验证

1. [发布契约](../../apps/backend/src/llm_d_lens/benchmark_rag/publication_contracts.py)：显式公开来源批准、固定版本完整清单、解析结果、文件/成员映射、逐类型计数和发布历史；使用完整载荷哈希生成确定性身份，不复用会脱敏字段的部署哈希。
2. [来源与归档](../../apps/backend/src/llm_d_lens/benchmark_rag/sources.py)：固定来源及路径/格式允许列表；离线 Git 按完整 commit 读取整棵固定树，保留二进制原始字节，不读取脏工作区；Prism 本地快照与人工审核的路径/哈希清单比对。缺失、额外、截断、符号链接和哈希不符均显式失败，不当作删除证据。
3. **不可变原始字节：**复用共享持久化存储和 artifact 注册，按 SHA256 归档；排他发布并同步到磁盘，读取时校验哈希，既有字节不覆盖。没有实现删除或 GC，保守地无限期保留历史，满足至少 365 天及保留引用依赖的要求。
4. [发布服务](../../apps/backend/src/llm_d_lens/benchmark_rag/publication.py)与[发布 DAO](../../apps/backend/src/llm_d_lens/db/dao/rag_publication.py)：原子暂存、完整成员与依赖闭包校验、未变化旧固定版本复用，以及同源行锁和指针/递增 generation 双重比较。激活重新校验源字节，在同一事务中写删除标记、切换指针并记录历史；失败保留旧版本。
5. **回滚与可见性：**回滚只能选择曾发布的清单，并要求当前 generation；防止回滚后的旧暂存版本误激活。来源禁用立即隐藏成员。内部列表/计数在单条 SQL 中经当前已发布清单过滤，发生在全文排序/计数前，不能通过把独立条目标成 `published` 绕过成员关系。只有注册行的指针和操作状态可变，证据与清单保持不可变。
6. [发布测试](../../apps/backend/tests/unit/db/test_rag_publication.py)：**35 项通过**，覆盖准备/切换失败、同源竞争、重复暂存与激活幂等、完整空清单退役、跨文件引用失效、旧字节保留、回滚、禁用、截断盘点和二进制归档；SQL 捕获确认发布/退役/回滚流程不访问 Agentic 记录表。
7. **最终回归：**整个数据库单测目录 **133 项通过**，包含隔离 PostgreSQL 16.2 上的迁移、存储、发布和现有数据库行为回归。Ruff 检查/格式化、文档链接、仓库英文检查、Git 差异空白检查通过；发布服务与 DAO 的 Pylance 语法检查通过。测试有 10 条既有 FastAPI/Starlette 弃用警告，不代表问答端到端验收或完整 Pylance 类型检查。
8. [数据库说明](../../apps/backend/src/llm_d_lens/db/README.md)已同步发布契约、权限边界、保留策略及测试方式；第 1 步通用 DAO 仅提取事务内共享写入逻辑，原有不可变写入规则和外部方法签名不变。

**第 2 步边界：**仅为内部管理库，来源批准不能来自 LLM 或未认证用户输入。`complete=True` 是可信适配器完成盘点后的断言，不是外部调用者声明即可成立的证明；未来网络适配器必须自行验证分页和下载完整性。当前没有网络采集、实际语料解析/导入、HTTP/MCP 入口或刷新触发器。内部成员过滤不是调用者授权检索，第 5 步仍须实现权限检查。没有 schema/迁移变更，没有对业务数据库执行操作；原有未跟踪的 `server/` 保持不动。

### 第 3 步已交付内容与验证

1. [Prism 适配器](../../apps/backend/src/llm_d_lens/benchmark_rag/prism.py)：消费现有 `normalize-and-merge.jq` 生成的归档结构和合并 ID 清单，不重写或执行规范化脚本。历史实验引用原始归档行；阶段实验引用原始完整运行 JSON，保留合并文件的路径/哈希/行指针，并与原报告身份、工作负载核对。
2. [初始固定输入](../../apps/backend/src/llm_d_lens/benchmark_rag/prism_initial.py)：记录原方案审核过的 2026-10-13 快照 22 份文件哈希和初始 249 条实验门槛。导入 API 每次接收审核清单和预期实验数，249 不是未来刷新时写死的永久总数；测试中的两条实验快照同样可以导入。
3. **精确值与单位：**JSON/YAML 数值直接解析为 Decimal，保留原始报告数值及单位；不把 jq 浮点转换产生的毫秒舍入值当作原始测量。显式新增 `s/token -> ms/token` 单位规则，保持 token 分母；单位归一化另存。未知/缺失单位不猜测，缺失统计量不补造，零值保留；指标和统计量别名不重复计数，冲突显式失败。
4. **溯源发现与处理：**ZIP 导出会重新编号报告，不能把导出的 stage 文件名当成实验 stage。适配器使用安全 YAML 解析，以完整报告内容与原始 JSON 一对一核对，再保存准确 ZIP 成员路径和哈希；缺失、歧义或内容不符均阻止发布。ZIP 不解压到文件系统。
5. **时间与上下文：**72 条阶段记录标记为 `submission` 时间，不当作实验发生时间；177 条历史记录缺少已核验的时间种类/时区，不猜测，原始时间保留在配置/溯源中。未知 concurrency 保持 null，目标 QPS 与实测 request rate 分开。四份运行级摘要仅生成上下文 passage，不新增实验或独立测量。
6. **真实快照验收结果：**隔离 PostgreSQL 中发布 **22 个来源文件、249 条实验（177 历史 + 72 阶段）、9,060 条测量叶子、4 条摘要上下文**。全部测量逐条按 JSON pointer 回读原始数值；固定问题中的 `02c8f038-1cad-4ee8-a72e-724affa923f4` 吞吐为 **6131.06177725188 output tokens/s**，不重复计入 `output_tput`。第二次导入不增行、不改变发布指针/generation。测量叶子总数不等于独立观测数，也不声称已完成跨 ID 的语义去重。
7. [Prism 验收测试](../../apps/backend/tests/unit/db/test_prism_import.py)：**20 项通过**，覆盖完整真实快照、幂等、精度、摘要去重、统计量别名、缺失单位/并发量、时间语义、未完成分页、重复 ID、非法/非有限数值、版本/工作负载/ZIP 不符，以及失败后保留旧发布版本。SQL 捕获确认真实导入全过程不访问 Agentic 记录表。
8. **最终验证：**整个数据库单测目录 **153 项通过，无跳过**，包括本轮真实快照验收；Ruff 检查/格式、文档链接、仓库英文检查、差异空白检查通过。新适配器与测试的 Pylance 语法检查通过；IDE 问题检查未报错误，但独立 Pylance LSP 仍出现 `reportMissingImports` 环境解析问题，不能据此声称完整类型检查通过。测试有 10 条既有 FastAPI/Starlette 弃用警告。
9. [数据库说明](../../apps/backend/src/llm_d_lens/db/README.md)已补充导入 API、来源/单位/时间语义、固定输入与测试方式。

**第 3 步边界：**没有新增 schema/迁移、依赖或网络源，没有对业务数据库执行迁移/导入，没有启用查询 API、用户权限或刷新触发器，也不接入 Agentic 规划。真实导入仅在用户选定的隔离验收环境中执行；测试 schema 删除、实例停止，临时文件遵循 pytest 保留策略。原始本地快照保持不变且仍被 Git 忽略。生产来源批准仍须由实际管理调用者明确提供，测试批准不能当作生产批准。第 3 步未提交、未推送，原有未跟踪的 `server/` 未触碰。

### 第四步前补充：目标机一键重建、备份和增量迁移

**用户确认的执行边界：**

- 在目标机器本地执行命令，读取已传输的文件，不从当前机器自动 SSH 推送/执行。
- 只迁移 `rag_items` 的全部历史、发布状态及原始证据，不迁移其他 Lens 业务表或数据库凭据。
- 采用 RAG 专用逻辑备份包，不要求任意原生 `pg_dump`/`pg_restore` 格式。
- 可以向非空 RAG 表增量更新：保留目标旧历史，补入缺少版本；同版本内容冲突时整笔拒绝，不静默覆盖。
- 首次恢复该备份时切换到备份的已发布版本；目标禁用的来源不能自动启用，备份中禁用的来源也保持禁用。

**已实现：**

1. [管理命令](../../apps/backend/src/llm_d_lens/benchmark_rag/admin.py)：`rebuild / backup / verify / restore`，显式要求数据库环境变量与持久化证据目录。失败非零退出，成功输出 JSON；不回退到默认/嵌入式数据库，不回显 DSN 或密码。
2. [逻辑包与证据迁移](../../apps/backend/src/llm_d_lens/benchmark_rag/transfer.py)：单条 PostgreSQL MVCC 查询取得一致的全量 RAG 快照，包含暂存、历史、删除标记、注册/发布清单；备份包包含规范化 DTO JSONL、清单和按 SHA256 寻址的原始字节。Decimal 精度不丢失，不包含可执行 SQL，也不复制机器绝对归档路径。
3. **备份和校验：**输出文件排他创建，不覆盖旧备份；校验版本、哈希、数量、来源批准、依赖闭包与历史完整性；拒绝重复、缺失或危险 ZIP 成员，不按备份路径解压。默认未压缩上限 1 GiB，可显式通过 `--max-bytes` 调整。哈希只证明完整性，不能替代对备份来源的信任。
4. [迁移 DAO](../../apps/backend/src/llm_d_lens/db/dao/rag_transfer.py)：目标支持空表及非空表。在同一事务中锁定受影响来源、插入缺少依赖、保存迁移收据并切换指针；本地 generation 递增，原机器历史独立保留，连续迁移后仍可回滚到旧发布版本。
5. **目标策略优先：**保留目标已有来源批准；导入清单必须满足该路径/解析器策略。任一侧已禁用即保持禁用。不相关来源及目标自己的旧版本不删除。重复恢复同一个备份不再次切换发布版本，即使目标随后又发布了新版本。
6. **初始化限制：**`rebuild` / `restore` 的显式 `--initialize` 只为全新独立 RAG schema 创建已审核的 `rag_items` 结构及索引/约束，不初始化整个 Lens，不 stamp Alembic；已有 Lens 管理库缺表时要求先走审核迁移。已有表不兼容则报错，不自动 ALTER。没有修改模型、增加表列或新增迁移文件。
7. **文件与事务边界：**先校验并不可变地复制证据，再执行数据库事务。数据库合并失败时旧库状态保持不变，但新复制且未引用的 blob 可能留存，不会冒险删除其他操作可能引用的文件。既有损坏证据不覆盖。
8. [迁移测试](../../apps/backend/tests/unit/db/test_rag_transfer.py)：**20 项通过**，覆盖新库、非空增量、二次迁移历史、禁用策略、内容冲突、指针故障整笔回滚、提交前旧版本可见性、校验损坏包、只创建 RAG 表、全文索引重建及 CLI 错误不泄漏凭据。
9. **真实快照跨目标验收：**通过 CLI 重建 249 条实验、9,060 条测量、4 条摘要及 22 个文件来源，再导出并恢复到另一独立 PostgreSQL schema；首次迁入 **9,337 行**（包括注册/清单两行），重复恢复插入数和更新来源数均为零。此处是在本机模拟不同目标，不是已完成真实远程网络部署。
10. **第 3a 工具阶段回归：**当时数据库单测目录 **173 项通过，无跳过**；Ruff、语法、IDE 问题检查、文档链接、仓库英文及 Git 空白检查通过。独立 Pylance LSP 依赖解析环境问题仍在，不声称完整类型检查通过。10 条既有弃用警告未改动。第 4 步最新结果见下节。

**目标机命令示例：**先在目标机安装本版本代码和后端依赖、配置 `LLM_D_BENCH_DATABASE_URL`，再运行下列任一命令。PostgreSQL 数据库与 schema 需由管理员预先创建；正式批准文件需填写真实审核信息。完整参数、批准 JSON 与托管/独立库区别见[数据库操作说明](../../apps/backend/src/llm_d_lens/db/README.md)。

```bash
PYTHONPATH=apps/backend/src .venv/bin/python -m llm_d_lens.benchmark_rag.admin rebuild --snapshot private/prism-public-2026-10-13 --approval /secure/prism-approval.json --data-dir /srv/lens-data
PYTHONPATH=apps/backend/src .venv/bin/python -m llm_d_lens.benchmark_rag.admin backup --data-dir /srv/lens-data --output /backup/rag-backup.zip
PYTHONPATH=apps/backend/src .venv/bin/python -m llm_d_lens.benchmark_rag.admin verify --bundle /backup/rag-backup.zip
PYTHONPATH=apps/backend/src .venv/bin/python -m llm_d_lens.benchmark_rag.admin restore --bundle /backup/rag-backup.zip --data-dir /srv/lens-data
```

对全新独立 RAG schema，重建/恢复时另加 `--initialize`；schema 非 `public` 时显式加 `--schema`。代码需要另行传输；Prism 原始文件被 Git 忽略，不随提交自动到达目标机。逻辑备份包则自带全部所需原始证据。**当前没有真实远程或业务库操作，没有自动上传或推送；第 3、3a、4 步代码现已按用户指令本地提交为 `1c43ec9`。**

### 第 4 步已交付内容与验证（review 通过）

1. [仓库导入器](../../apps/backend/src/llm_d_lens/benchmark_rag/repository_corpus.py)复用来源批准、完整 Git 清单、原始字节归档与原子发布。读取固定 commit，而非脏工作区；不联网、不执行文档中的示例命令，不扩大已批准来源或路径。
2. [初始清单与图审核](../../apps/backend/src/llm_d_lens/benchmark_rag/repository_initial.py)固定 `llm-d` 的 `080c14d957755da4cc363745638619fc748558f1`：97 份 Markdown（含 20 份结果文档）、5 份自定义 JSON、37 张 PNG，共 139 个文件；固定 `llm-d-benchmark` 的 `6e081de16d2debc7b29436ad53f2c62e8d475f2e`：27 份 docs 文档加报告 README，共 28 个文件。清单 SHA256 和逐类型数量独立核验。
3. **精确段落：**保留原文、标题层级、固定版本的字符偏移/行范围、文档链接及图片引用/图注；代码围栏中的标题不当成正文标题。基准文档与 JSON/YAML 示例不生成实验或测量。
4. [表格适配器](../../apps/backend/src/llm_d_lens/benchmark_rag/guide_tables.py)审核全部 97 份指南的 **155 张 GFM 表格**，不仅处理结果目录，也覆盖正文中的实际结果。输出 **421 条来源范围实验、1,559 条精确测量、475 条审计上下文**。每个数值追溯到表/行/列/单元格，复合数值带子值位置；保留场景、配置、工作负载与限定条件。
5. **不提升为精确观测：**配置/参考表、已计算的差异、近似值和范围、缺失值、空延迟直方图、无法安全解释的历史/叙述证据保持上下文并给出原因。混合模型和 GLM 多场景分开，不因文件名或硬件相似合并。重复刊载的表格不视为新增独立实验。
6. [自定义 JSON 适配器](../../apps/backend/src/llm_d_lens/benchmark_rag/guide_json.py)分别处理五份 GLM 数据，输出 **24 条来源范围实验、594 条测量、5 条上下文**。Decimal 与原始 JSON pointer 逐值往返；配置不是指标，重复和聚合记录明确标注非独立，不把它们套用成 Prism v0.2 报告。未知硬件/运行时保持未知，未声明的吞吐分母不猜测。
7. **图上下文：**保留全部 37 张图，审核为 **15 张 partial、3 张 summary_only、19 张 unknown**，不从像素生成数值。480 条类型化图关联只依据明确引用图的报告与已审核表格场景建立；GLM 数据不误关联 GPT-OSS 图。Qwen TPU 缺失曲线点仍不能精确列出。
8. **初始发布验收：**

   | 来源 | 文件来源条目 | 实验条目 | 测量 | 段落 | 图 | 图关联 |
   | --- | ---: | ---: | ---: | ---: | ---: | ---: |
   | llm-d 指南 | 139 | 445 | 2,153 | 1,468 | 37 | 480 |
   | llm-d-benchmark 文档 | 28 | 0 | 0 | 392 | 0 | 0 |

   上表不包括操作注册/发布清单行，也不是独立实验计数；摘要、重复报告、聚合记录的语义去重没有宣称完成。两个来源分别原子发布，不承诺跨来源共同事务；第二个来源失败不会自动回退已发布的第一个来源。
9. **真实输入校验：**隔离 PostgreSQL 发布、重复导入不增行/不改指针或 generation；H200 GPT-OSS 权重加载样本精确保留 **0.93 s / 30.3 s**。新语料逻辑备份后恢复到另一独立 schema/证据目录，发布 DTO 一致、全部原始字节哈希一致，重复恢复无更新。没有执行真实远程迁移。
10. **测试：**[Markdown 表格测试](../../apps/backend/tests/unit/db/test_guide_tables.py) **57 项**，[JSON 测试](../../apps/backend/tests/unit/db/test_guide_json.py) **37 项**，[仓库集成测试](../../apps/backend/tests/unit/db/test_repository_corpus.py) **14 项**。完整数据库目录 **281 项通过、无跳过、10 条既有弃用警告**；随后增强的两项真实仓库恢复验收也通过。最终 Ruff 检查/格式化、Pylance 语法、IDE 问题检查、文档链接、仓库英文及 Git 空白检查通过；不声称完整 Pylance 类型检查已通过。
11. **后续版本保护：**产生测量的 Markdown 与自定义 JSON 元数据仅审核当前固定 `llm-d` commit；换提交须重新审核元数据/解析器兼容性，不能拿相同文件名沿用旧模型/场景假设。无测量的普通上下文可处理其他有效完整提交。尚未实现自动刷新。

**第 4 步边界：**没有新增模型、schema、迁移或依赖；当时未修改 Agentic 记录/规划、未开放 REST/MCP 查询、未增加或分配 RAG 查询权限。可调用 `stage_repository` 暂存或 `import_repository` 发布；正式调用须使用真实来源批准，操作示例见[数据库说明](../../apps/backend/src/llm_d_lens/db/README.md)。现有 CLI 的 `rebuild` 仍只重建初始 Prism，`backup/restore` 已验证兼容新语料。代码现已随 `1c43ec9` 本地提交、未推送，未跟踪的 `server/` 未触碰；本文仍仅本地保留。

### 第 5 步已交付内容与隔离验收（review 通过）

1. **统一契约与服务：**[查询 DTO](../../apps/backend/src/llm_d_lens/benchmark_rag/query_contracts.py)、[只读检索 DAO](../../apps/backend/src/llm_d_lens/db/dao/rag_query.py)和[确定性问答服务](../../apps/backend/src/llm_d_lens/benchmark_rag/service.py)提供同一公开分支；REST 为 `POST /api/v1/benchmark-rag/query`，只读 MCP 为 `query_public_benchmark_rag`。MCP 转发真实调用者和相同请求，不另外实现检索，不替换为服务身份。
2. **用户已选择的权限策略：**默认仅全局管理员获得 `benchmark-rag:query:read`；其他角色须显式全局授权。集群范围授权、只有 MCP 调用/Playground 权限、匿名、关闭认证后的 system 身份及须改密账号均不能读取 RAG。MCP 还须通过原有工具调用策略；浏览器 POST 保持 CSRF 校验。路由先授权再构造服务，服务自身也再校验，未授权不会查询证据或因构造服务而初始化存储。
3. **结构化与全文查询：**支持来源、固定版本/路径、实验/Prism ID、模型/硬件/运行时、workload/series、指标/统计量/单位及数值范围。范围必须显式声明单位；原始 Decimal 与有明确规则的规范化列分开，吞吐分母不混同。段落使用 PostgreSQL 全文搜索；图的条件过滤依赖当前已发布的类型化关联，不按名称猜实验，也不因一图多实验重复计算图数。
4. **证据与撤销：**SQL 在排名、计数和返回片段前限定已批准、启用来源的当前发布清单；暂存行、旧发布和未进入清单的“published”行均不显示。读取时验证原始字节哈希和 JSON 数值/table cell/段落偏移，返回固定路径、版本、哈希、artifact URI、定位 pointer 和实验条件；输出前复查 generation。查询中撤销/切换返回冲突，缺失/损坏归档返回显式 503，不伪装为成功空结果。
5. **无 LLM 依赖：**`generation_mode=deterministic`，不执行检索文档中的指令；测量、正文/示例与图片上下文明确区分。缺失条件/比较或未发布曲线点拒答；不会平均冲突值、估计曲线点或把相似模型视为同一实验。返回计数仅表示索引条目，不是独立实验数或跨来源语义去重。
6. **真实三源问答验收：**在独立 PostgreSQL schema 与临时证据目录导入已固定的全部三源后，验证 Prism `02c8f038-1cad-4ee8-a72e-724affa923f4` 的 Qwen3-0.6B 吞吐 **6131.06177725188 output tokens/s**；H200 GPT-OSS 权重加载 **0.93 s / 30.3 s** 及 warm NFS/跨节点条件；benchmark report 用于聚合结果与工作负载/环境统计而非逐请求详细 trace；Qwen Coder TPU 缺失曲线点明确拒答。实际 H100 图关联与未知硬件条件过滤通过；全部 **2,153** 条已审核指南数值都通过问答运行时原始 token 校验。
7. **接口与测试：**新增 PostgreSQL/服务/真实 REST 契约测试 **25 项通过**；MCP/生成器/调用授权 **27 项通过**，覆盖实际 HTTP gateway→proxy、真实签名身份转发、权限分离、身份伪造拒绝、关闭认证拒绝和 Decimal/图关联原样传递。路由新增工厂访问探针后 **60 项聚焦测试通过**。数据库、认证与问答合并回归 **564 项通过、无跳过，26 条依赖/迁移警告**；收紧英文关键词规则后，最终查询与路由聚焦回归 **85 项通过**。Ruff、服务端类型检查、定向 ESLint、双语文档检查/构建、仓库英文检查和空白检查通过。IDE/Pylance 连接不可用，不能声称 Python 完整静态类型检查通过；文档构建保留既有 `OWNERS` 链接警告。

**第 5 步历史边界：**当时只实现公开分支，没有读取/写入 Agentic 记录；现已通过 review 并本地提交为 `86ef3f6`，未推送。第 6 步的可选内部读取见下节，默认请求仍保持公开来源。确定性自动分类采用有限英文规则，不是通用自然语言解析；中文等其他语言或精确条件请显式指定 `intent`/过滤字段，并检查响应 `filters`。各实例仍同时需要 PostgreSQL 与保留的证据归档；未实现共享卷或真实部署。

### 第 6 步已交付内容与隔离验收（review 通过）

1. **用户选择显式 opt-in：**同一 REST `POST /api/v1/benchmark-rag/query` 和只读 MCP `query_public_benchmark_rag` 接受 `include_internal=true`。默认 `false`，不读取 Agentic/evaluation 记录。保留原路径和工具名以兼容现有调用；内部开关不是权限授予，也不启用部署规划器工具。
2. **已核实并保守处理的历史问题：**现有 `agentic_benchmark_records.evaluate_run_id` 仅由 `benchmark_id` 解析，且规划历史写入器实际写入 `evaluate:<8位 simulation task ID>`，不能认定该 ID 是 Evaluate 归属。新分支只接受规范 `evaluate:<UUID>` 与真实存活 `evaluate_runs` 的一致关系；旧 simulation 历史保持不可检索，不修改原生产者、数据或排序，也不猜测回填。
3. **独立只读授权查询：**[内部检索 DAO](../../apps/backend/src/llm_d_lens/db/dao/rag_agentic_query.py)在 SQL 中验证 evaluation、真实存活 owner 用户及 typed/legacy JSON 归属/cluster/id 不冲突，并在排序、计数、分页及记录离开分支前应用原有 `evaluate:run:read` 的 action/scope/ownership 规则。继续要求 RAG 全局特性权限；MCP 还需原有调用策略。无归属、已删除或无法证明关系的记录，即使全局管理员也不显示；没有 Evaluate 读取权则只得到公开结果，不泄露隐藏记录存在性或数量。
4. **撤销与读后复查：**内部请求验证当前用户存活、激活状态、权限版本和改密状态；发出结果前重新检查 evaluation 归属、版本、访问权限及存在性。过期 principal、查询期间删除/归属变更或撤权返回冲突，不发出旧证据。公开来源继续执行原有归档哈希与发布 generation 复查。
5. **内部证据范围与精度：**[指标契约](../../apps/backend/src/llm_d_lens/benchmark_rag/agentic_metrics.py)只返回已有成功记录中的 TTFT p95、TPOT p95、E2E p95、未声明统计量的 throughput 和 error rate；不使用预测/插值。明确标记 `agentic_measurement`、owning evaluation API URI、benchmark/run/cluster、时间戳、字段与规范化记录校验和。原记录为 Float，响应 Decimal 字符串不代表恢复原始报告精度，citation 显式写入 `numeric_precision=stored_float`；公开数值仍验证原始 token。
6. **验证后合并，而非名称去重：**[内部证据与合并](../../apps/backend/src/llm_d_lens/benchmark_rag/agentic_answers.py)要求固定公开 Prism 原始 JSON 对象本身包含规范 `evaluate:<UUID>`，且已授权内部值的 metric/statistic/unit/value 与已报告模型/硬件/运行时/workload 一致，无未解释的 series。符合时返回一个 `public_and_agentic` observation，保留公开 citation 与 `corroborating_citations`；裸 UUID、相似模型、未知关系或冲突值保持分开，不平均、不声称全语料语义去重。无权看到的记录不贡献内部 provenance 或 overlap 状态。
7. **清晰计数与边界：**`matched_items` 继续表示候选项数（公开索引条目 + 已授权内部 benchmark records，合并/指标展开前），不是独立实验数；`returned_observations` 表示已返回且验证合并后的 envelope 数。两分支合计输出遵守 `limit=1..50`，截断明确提示，不能把局部窗口当成全库 distinct-run 总量。definitions/figures 仍从公开分支读取；内部不匹配 public source_path/revision/series，workload 只接受存储契约字段，不能忽略必需条件。
8. **验证：**新增授权 DAO **46 项隔离 PostgreSQL 测试通过**；服务/REST 真实数据库集成 **9 项通过**；内部数值/合并/opt-in 聚焦 **14 项通过**，最终合并运行共 **69 项通过**。原公开三源问答 **25 项继续通过**，包含全部 **2,153 条**指南数值原始 token 验证。完整数据库/认证/RAG 合并回归 **630 项通过、无跳过，26 条既有依赖/迁移警告**；其后 DAO 简化完成的最终版本由前述 69 项聚焦回归再次验证。Node 服务端完整回归 **136 项通过**，其中 MCP 分支 **11 项**、独立生成器 **3 项**通过；服务端 typecheck、定向 lint、双语文档构建、文档链接、英文和空白检查通过。Pylance 部分模块无诊断，但仍有依赖解析环境问题，不声称完整 Python 类型检查通过；既有 `OWNERS` 文档链接警告仍在。

**第 6 步边界：**没有新表、迁移或依赖；不复制公开证据到 Agentic，不修改现有记录与规划器、不回填旧 simulation 历史；未操作业务库或远程集群。此次完成的是可验证归属记录的可选查询能力，不承诺已有安装一定有符合条件的内部记录。代码已通过 review 并本地提交为 `3ab782d`，未推送；入库完善另列第 6a 步规划，第 7 步及部署验收未开始。

### 第 6a 步：agentic_benchmark_records 与 simulation task 入库完善（仅规划）

**2026-10-20 执行顺序变更：**按用户明确指令跳过此补充步骤，保留以下规划供以后独立恢复；不阻塞第 7/8 步，不能因为跳过就把旧 simulation 历史视作已具备可验证 Evaluate 归属。

**范围已更正：**用户确认目标表是 `agentic_benchmark_records`，本计划聚焦其入库逻辑、`simulation_tasks` 生命周期以及两者之间的来源/归属关联；适用时保留真实 Evaluate 归属上下文。不额外扩展到 deployment batch/job/execution 或 Agentic 规划/session 的持久化改造。

**已观察到的入口，不作为本轮修复结论：**[simulation DAO](../../apps/backend/src/llm_d_lens/db/dao/simulation_task.py)调用 DTO→Row 的 create/upsert，[simulation row](../../apps/backend/src/llm_d_lens/db/models/simulation_task.py)已有 owner 列，但仅有列不能证明创建主体贯穿写入及重载；逐请求结果与 extra 元数据有文件后端。现有 [Agentic facts](../../apps/backend/src/llm_d_lens/agentic/facts.py)在读取规划历史时把 simulation task 规范化为 `evaluate:<task_id>`，再经 [benchmark store](../../apps/backend/src/llm_d_lens/agentic/benchmark_store.py)与 DAO 写入。需要完善的是端到端生命周期、归属与证据生产链，而不只是把数据塞进某张表。

保留第 7/8 步编号，将新增工作拆为以下独立 review 阶段；本轮不授权实施、迁移或数据回填：

| 阶段 | 计划内容 | 验收门槛 |
| --- | --- | --- |
| **6a.1 契约与写入链路核对** | 逐条追踪 benchmark 生产入口、Evaluate、simulation 的 create/save/complete/retry/delete；列出 benchmark/run/task/stage ID、真实创建主体、owner/cluster 来源、DTO/Row 映射、原始证据引用。明确来源类型、关联基数、生命周期归属与测量 provenance，不靠 ID 前缀判断。 | 范围、权限、字段权威、未知值、删除/保留语义和兼容迁移提案先 review；需新表或迁移时单独批准。 |
| **6a.2 源记录生命周期与证据持久化** | 真实调用者归属和已核验 cluster/context 贯穿服务、DTO、DAO、reload/rerun；拒绝客户端伪造或冲突身份。审核真实 evaluation/simulation 来源→benchmark 证据的关联，不因使用某 endpoint 就继承其 owner。区分实际观测与规划推断/默认模型、硬件、runtime、workload、配置。定义终态、并发保存、重启、重试与幂等规则。文件侧逐请求/extra 要有持久引用、哈希、原子发布/恢复、缺失/不完整状态、备份恢复与保留策略。 | 创建、重载、完成、重跑、崩溃恢复均保留真实归属、条件与证据完整性；数据库/文件故障不能呈现为“完整观测已入库成功”。 |
| **6a.3 正确的规范化证据入库** | 区分 simulation/evaluation 的 `source_kind`、source ID、run/stage 与 owner context；不再把 simulation ID 填成 Evaluate 软引用。评审版本化适配器和必要的增量迁移。保留 metric/statistic/unit/分母、精度、提取版本和原始引用，未知值不填成观测。评估将证据生产由“规划事实读取时顺带写入”转到完成事件或可重试 ingestion Job，明确幂等键、重放与显式失败。 | 同一真实来源完成事件的重复/并发执行只形成正确的来源范围观测；失败/部分/歧义输入有明确隔离或标记。任何影响规划器的改动须独立回归与 review，不能借 RAG 修复顺带改变排序。 |
| **6a.4 历史修复与来源专属授权接入** | 先 dry-run 分类可证明、冲突、孤儿、不可恢复记录；仅凭保留原始证据修复，保留映射/审计，幂等重跑并有回滚或恢复策略。不能把 8 位 simulation ID 改名伪装成 Evaluate UUID。如需让 simulation 来源的 benchmark 证据可查询，先验证其真实 owning resource 的既有权限与 scope/owner 规则，新增来源专属 SQL 授权，不放宽当前 Evaluate-only RAG gate。 | 跨 owner/cluster、归属删除、撤权、默认关闭/显式开启、隐藏计数/片段/overlap、REST/MCP 一致性、来源身份去重、备份恢复与规划器非回归通过。无法证明的旧记录继续排除，并有管理员可见审计报告，不静默归到某用户或假报迁移成功。 |

**实施依赖：**建议先完成 6a.1 的契约 review，再依次推进其余阶段；要宣称新入库的 simulation 来源 benchmark 历史能安全用于内部 RAG，必须先通过 6a.4 的来源专属授权验收。该计划不阻塞已有公开来源查询或将第 7 步标为已启动。不得把规划中的字段名称当作已批准 schema，也不自动把 simulation task 记录视作 measured benchmark。

### 第 7 步：刷新场景与触发机制（review 已通过）

1. **共用入口：**[刷新服务与契约](../../apps/backend/src/llm_d_lens/benchmark_rag/refresh.py)提供严格 `RefreshPlan` 和 `RefreshReport`；[管理 CLI](../../apps/backend/src/llm_d_lens/benchmark_rag/admin.py)新增 `refresh`、`status`、`rollback`。Prism 仅允许 manual；仓库按用户选择同时支持 manual/commit/periodic，复用同一固定版本入口，不自动 fetch 或追踪分支。
2. **审核计划与触发：**固定 origin/revision、完整有序路径/哈希清单、来源审批、逐类型计数、base manifest/generation、触发方式与图审核。commit 事件必须等于审批的完整 SHA；periodic 重跑同一计划，不自动批准新版本。空清单删除全部仓库文件需显式 `allow_empty`。
3. **新数值版本：**指南 table/JSON 保持默认拒绝未审核版本。新增精确 revision 的 metadata 兼容 review 入口；CLI 仅在计划具备 `metadata_review_reference` 时传入，先核验完整 inventory digest。审核必须确认固定适配器的模型、硬件、runtime、workload 等语义仍成立；新布局/schema 仍报错，不能只改文件名或复用旧假设。
4. **安全与恢复：**沿用不可变归档、暂存→原子发布与 generation/CAS。直接重跑 no-op；旧计划不能覆盖新发布、rollback 或 disable/enable。并发采集/发布/策略变化显式 conflict。旧引用保留字节，新查询仅看当前成员；撤销后下一次查询不可见。失败可留下不可检索的 staged 行/归档，不能宣称所有暂存均清理。
5. **结果与运维：**成功输出 published/unchanged JSON，含审批引用、计划/清单哈希、revision、时间戳、文件/逐类型计数及 publication IDs/generation；失败退出 1，stderr 输出 failed JSON。成功发布历史在 registry 中持久化，失败尝试由外部调度器持久保存 stdout/stderr；不是新增持久队列。CLI 不开放给 MCP/REST 写操作。
6. **验证结果：**受影响导入/发布/迁移/查询/路由与新增刷新回归 **309 passed，10 warnings，零跳过，284.85 秒**；其中新增刷新入口/生命周期测试 27 项。实际本机 fixture Git 仓库与隔离 PostgreSQL 覆盖两仓库新增/删除/重命名/标题和正文变化、真实布局表值与 JSON 数值修改、Prism 新快照/缺失文件/哈希错误/截断分页、解析/计数失败保留旧发布、重复/并发/撤销/回滚与旧引用。原有真实固定语料测试包含在本次运行中。Ruff、格式、英文与文档链接检查、中英站点构建通过；保留原有 `resources/OWNERS` 断链警告。IDE problems 无错误，Pylance 仍报 pydantic 的 `reportMissingImports` 环境解析警告，不宣称完整 Python 静态类型检查通过。测试 schema/实例自动清理，临时证据遵循 pytest 保留策略。
7. **部署边界：**提供可信 CI 与 cron/管理 Job 共用命令及中英操作文档；未安装真实触发器、定时进程、CI secret 或集群 Job，未接触业务库/远程集群；不修改 6a 入库逻辑、规划器或 `include_internal` 默认值。第 7 步结束请用户 review，再进入第 8 步基线评估；新来源仍须独立审批。

### 第 8 步：固定问答基线与额外来源接入门槛（review 已通过）

1. **固定可复现用例：**[评估契约与 runner](../../apps/backend/src/llm_d_lens/benchmark_rag/evaluation.py)接受版本化用例集，固定问题/条件、预期状态/拒答、精确 Decimal、证据类型与来源、计数、禁止泄漏片段；报告绑定用例集哈希及来源版本/清单哈希。预期结果不从实际答案自动生成。
2. **独立验证而非自证：**必传 verifier 对每一条主引用及 corroborating 引用核验实际保留字节/DB 记录、值/单位/条件/指针、manifest 与真实 owner。runner 同时检查确定性测量回答行与证据一致、整体 limit、重复身份、opt-in、预期 domain error；实现异常直接抛出，不转为“成功空结果”。全部用例必须通过，缺真实输入的 skip 不能当验收通过。
3. **内部基线已通过：**固定 12 案例，在隔离 PostgreSQL 中创建两个真实 owner/Evaluate、规范化记录及公开 overlap fixture。验证默认仅公开、授权内部、双引用合并、另一 owner 隔离、隐藏与缺失 ID 等价、无 Evaluate/RAG 权限、匿名、过期 principal、孤儿/旧 simulation 记录排除。12/12 通过，6 个数值封装、7 条引用验证通过；数据库无数据写入，原 benchmark DTO 不变。这些 fixture 不代表修复了 6a。
4. **公开基线通过：**[公开固定用例](../../apps/backend/tests/fixtures/benchmark_rag/public_baseline_v1.json)覆盖全部 9 类场景，真实固定 Prism 与两个本地 Git 语料完成 **20/20** 回放，27 条引用、3 个数值封装核验通过。校验实际导入 manifest 的来源版本/两个 Git 清单 digest 和 Prism 完整固定文件哈希；正文切片、图引用/关联、测量指针和原始值均独立验证。包括 6131.06177725188 output tokens/s、H200 0.93/30.3s 与实际条件、文档定义/非测量示例、缺失 PNG 数值拒答、错误关联/不完整比较、撤销与新来源拒绝。另有伪造值/引用/上下文等 mutation 对照，确保 verifier 不会自证通过。
5. **报告与边界：**可通过显式 `LENS_RAG_BASELINE_REPORT_DIR` 保存 public/internal JSON 报告，包含时间戳、版本、用例/类别、通过/失败、引用/观测数量与问题码，不含原始私有回答、用户身份或凭据。`verified_citations` 统计主/佐证引用；`numeric_observations` 统计数值封装。不是通用自由文本 LLM 评委，也不证明真实线上部署。
6. **额外来源不自动接入：**基线通过后，新仓库/数据集、Prism GCS/GIQ、论文仍需单独审批可见性/许可/权限、固定 origin/path/revision/完整清单、来源适配器与原始字节 fixtures、问题/拒答/错误关联/撤销测试、manifest 发布与回滚。论文默认解释性，除非测量独立验证；发现 URL 不能自动授权。当前三个来源白名单不变。
7. **最终验证：**公开/内部基线、runner 自测、原有内部 DAO/service 权限回归共 **70 passed，3 warnings，零跳过，136.96 秒**。runner 自测覆盖错误数值/回答、隐藏标识泄漏、坏引用、预期拒绝、实现异常、无效 verifier、重复/矛盾用例；另验证重放与用例哈希保留未显式设置的 filter 语义，不改变默认来源规划。Ruff/格式、英文/空白/文档链接检查及中英站点构建通过；Pylance 仍存在 pydantic 环境解析警告，不宣称完整 Python 静态类型检查通过。
8. **可复用结果：**本 session 的 `files/rag-v1-evaluation/public-baseline.json` 与 `internal-baseline.json` 已持久保存机器可读结果，不进入仓库/commit。原始私有响应及用户身份未写入报告；测试 schema/实例自动清理，pytest 临时目录保留策略不等于真实在线数据库。

### 两种部署场景：现状与补充实施步骤（真实迁移已开始，schema 评审暂停）

**核心区别：**跨集群迁移生成两套独立数据；同集群共享让多个实例访问同一套数据。`rag_items` 与不可变原始证据必须一起考虑；只有共享/迁移表，不能代表完整 RAG 已交付。保留原第 0–8 步编号，在第 3 步后增加第 3a、3b 步跟踪部署工作，不改写历史验收，也不将规划当作已实现。

| 场景 | 当前已有能力 | 尚未交付 |
| --- | --- | --- |
| 集群 A → 集群 B，迁移 RAG | 目标机 CLI 和逻辑包，支持历史、发布状态、证据及非空目标；本机隔离验收与工具 review 已通过 | 真实两集群的部署/操作流程与验收；没有自动传输、SSH 或持续同步 |
| 集群 A → 集群 B，迁移整个 Lens PostgreSQL | 本轮没有实现；RAG 包不是整库备份 | 需另行审批全库方案，盘点业务表、schema/版本、账号角色、权限和其他关联存储，不列为 RAG v1 的已有或承诺交付 |
| 同集群多个实例共享 RAG | PostgreSQL 事务/锁与文件系统归档接口提供基础 | 同库同 schema 接线、共享证据目录、凭据/连接池、部署资源、管理任务与真实多实例验收；本次目标已选择源码进程，不是 Pod |
| 同集群共享面向用户的 RAG 服务 | REST/MCP 查询与授权已实现并本机验证；共享部署未验收 | 仍须真实调用者下的共享运行环境与证据存储验收；内部分支保持显式 opt-in 与所属上下文授权；共享表不等于租户隔离 |

#### 第 3a 步：跨集群迁移工具 review 与真实部署验收

**已完成且已通过 review 的工具阶段：**集群 A 的管理员运行 `backup`，把 RAG 全部行、来源/发布/删除历史及原始字节打入逻辑 ZIP；经管理员认可的渠道传到 B，在 B 运行 `verify`、`restore`，写入 B 自己的 PostgreSQL 与持久化证据目录。也可以把完整固定 Prism 快照和正式批准传到 B，运行 `rebuild`。两个集群之后独立运行，不自动同步。

**真实目标阶段已获授权并开始；当前因非 RAG schema 漂移暂停，详见下方当前停点：**

1. 先完成现有工具 review，再核对两端代码、数据库结构、账号权限、持久化目录及正式批准策略；目标数据库/schema 预先准备，托管 Lens 仍走审核迁移，不用独立库初始化绕过。
2. 补全两端运行手册：凭据经 Secret 等部署机制注入、受信备份安全传输、校验、恢复、失败诊断与切换/回退。若要最终切流，协调源端写入或取得最终一致备份；旧快照不会自动包含之后的写入。回退窗口需审批，不自动删除源数据。
3. 在两个真实集群分别验收原始快照重建、备份恢复，以及迁入非空目标。保留目标历史/批准，采用备份已发布版本，任一侧禁用不自动启用，内容冲突或指针故障整笔回滚；已复制未引用 blob 可能保留。
4. **验收门槛：**逐类型计数、精确测量样本、发布/历史、来源启停、原始字节哈希与旧引用一致；重复恢复无状态变化；断开源集群后目标仍能独立读取证据。第 5 步完成后补用户查询对比，并纳入第 8 步总体验收。

**不包含：**整个 Lens 数据库迁移、账号角色复制、自动 SSH、数据库服务自动创建、持续复制或自动故障切换。已有 20 项迁移测试、173 项数据库回归不替代真实跨集群验收。

#### 第 3b 步：同集群共享数据库与 RAG 证据

**当前状态：仅规划，未实现共享部署。**目标为多个实例连接同一 PostgreSQL database/schema 的 `rag_items`，并访问同一份持久化原始证据；不是每个 Pod 各自恢复一份数据库。

1. **先评审部署选择：**确定数据库/schema、命名空间访问、账号最小权限、总连接池预算、发布管理边界与共享证据后端。当前归档使用文件系统，可优先评估共享文件系统，但不能假设任意 RWX 卷都兼容；须核验硬链接排他发布、原子性、持久化、权限与可见性。对象存储/统一证据服务尚未实现，选择它们需另行批准适配方案。
2. **再接线与编排：**按仓库部署约定配置连接、Secret、共享卷/目录及受控管理 Job。集中执行导入、恢复、发布和迁移；查询实例按实际职责尽量只读，不能未经分析就假定整个 Lens 后端无需写其他业务表。不自动将其他业务目录全部共享。
3. **基础设施验收：**至少两个实例看到同一已提交版本，提交前没有混合版本；同源竞争和过期激活被正确处理；发布、禁用、回滚的影响在所有使用该注册来源的实例一致。Pod 重建及调度到另一节点后，历史原始证据仍可校验读取。存储不可达或内容损坏必须显式失败。
4. **服务验收依赖：**第 5 步完成后，以相同身份/过滤条件访问不同实例，验证证据、计数、权限撤销和 REST/MCP 一致性；Agentic 分支只有第 6 步通过后加入。基础设施可先验收，但此前不能宣称完整共享 RAG 服务已交付。
5. **隔离边界：**同库同 schema 的来源发布、禁用、恢复和回滚对所有共享者生效；若要独立发布或多租户，须另行设计隔离和应用授权，不能把 schema 名称或共享表当作权限实现。

**验收门槛：**部署配置与操作说明经 review，选定存储语义和最小权限通过验证，真实多 Pod/跨节点测试通过；用户级服务验收还必须通过第 5 步。

### 历史停点（记录于 2026-10-20）：第 3a 步目标预检与代码集成完成，数据库迁移等待 review

1. **目标与授权：**用户指定 `wenxin@10.112.229.74`，仓库为 `/mnt/disk1/wenxin/llm-d-lens`；核验后用户明确选择沿用现有源码进程部署，而不是 Kubernetes Pod。保留 `fix_gpu_runtimeclass` 分支；安全备份时 HEAD 已由外部提交推进至 `369ac0ab27ca0bc647e7b8d9a556ae50f73b5fec`，初始未提交的前端改动已经提交，不进行 reset、切分支、stash 或覆盖。
2. **已持久准备：**目标本地已完成整库安全备份、原代码/运行环境与原账号/凭据校验快照，位于 `/home/wenxin/.local/share/lens/backups/rag-3a-20261020`；这些私有备份不离开目标。另已校验传入 RAG-only 逻辑包（14,961 行、190 个原始 blob）及 22 个精确固定的 Prism 原件，不包含来源端账号、凭据或业务表。
3. **代码状态：**兼容目标较新代码的 RAG 补丁已通过统一检查并应用；五个共享接入文件保留目标已有逻辑。旧来源迁移 `7a12c8e49b60` 未复制到目标迁移链；按用户批准的方案，尝试从目标 `d9e3f7b1c5a8` 自动生成仅新增 `rag_items` 的原生迁移，未重放旧历史。
4. **明确阻塞：**Alembic 同时发现已有业务表 `audit_logs.cluster_id` 的数据库类型为 `VARCHAR(8)`、当前模型要求 `VARCHAR(32)`。非 RAG 操作保护立即中止；没有过滤掉差异继续执行，没有生成新迁移文件，也没有升级数据库。需要用户决定保留该历史差异、显式限定本轮仅创建 RAG 表，或先单独处理业务 schema 漂移。
5. **停止后的实测：**数据库版本仍为 `d9e3f7b1c5a8`，`rag_items` 仍不存在；用户 1、集群 2，Agentic benchmark/simulation/Evaluate 均为 0。原账号身份、密码哈希、状态及原凭据校验均保持不变；原三个服务进程未重启，Node HTTPS 与后端 HTTP 的 auth/providers 均返回 200；分支和 HEAD 未变，RAG 补丁可精确反向检查。
6. **尚未执行：**目标 Prism 重建/非空恢复/重复恢复、验收账号创建、服务重启、真实 HTTP/MCP 基线及来源断开验收均未执行，不能宣称第 3a 步完成。第 3b 步须在 3a 完成且用户 review 后继续，范围为同主机多个源码进程共享 PostgreSQL 与 RAG 证据；不代表多 Pod、跨节点或任意 RWX 存储已验收。6a 继续跳过。
7. **提交边界：**本轮未创建任何 commit 或 push；本文和本地英文规划继续忽略，不进入远端。SSH 密码和私有运行环境不写入仓库、脚本或本规划。

### 上一阶段：独立本机持久化单实例验收已 review，引用修复已提交

1. **当前进展：**第 7/8 步已本地提交 `c5ee9a1`；6a 继续跳过。用户确认“本轮 review 通过，提交引用修复但不推送”，引用空白修复、回归与说明已提交为 `c6f4796`。正式上线许可与对外发布仍需另审。
2. **后续顺序已更新：**本机单实例已 review；用户随后授权真实目标第 3a 步，再 review，再做第 3b 步源码进程共享；当前停在上方记录的 schema review。6a、额外来源、业务表变更、公开分发和 push 仍需单独批准。
3. **文档与业务边界：**本文及本地英文方案继续由已有忽略规则排除，不纳入 commit/push；附件未修改。现有本机 Lens 服务保持不变；新实例使用独立数据库、证据存储、配置、日志和端口，仅监听回环地址。实际结果见下节，不以历史单测代替真实验收。

### 本机持久化单实例真实验收记录（2026-10-20，review 通过）

**授权与隔离**

- 用户明确选择“当前机器上的独立持久化验收实例，不接触现有业务库”，并批准三组既定公开来源仅供本机验收；没有把单测审批复制为正式上线审批，没有扩大来源范围或对外分发。
- 入口：[Node REST/MCP](http://127.0.0.1:13000)，后端：[FastAPI](http://127.0.0.1:18081)。两者只监听 `127.0.0.1`；未另起前端开发服务，未重启原有 Lens。
- PostgreSQL 为独立持久化实例，数据库 `lens_rag_acceptance`、业务 schema `public`；设定随机强口令，实际错误口令连接被拒绝。
- 持久数据：`/home/wenxin/.local/share/llm-d-lens-rag-acceptance/`，数据库目录为其中 `postgres/`，证据归档为其中 `artifacts/benchmark-rag/`。
- 配置：`/home/wenxin/.config/llm-d-lens-rag-acceptance/`；缓存：`/home/wenxin/.cache/llm-d-lens-rag-acceptance/`；日志、备份和报告：`/home/wenxin/.local/state/llm-d-lens-rag-acceptance/`。根目录权限 `0700`，口令/密钥/登录交接文件权限 `0600`，未写进 Git 或聊天输出。
- 管理员交接文件为配置目录中的 `operator-login.json`，初始管理员口令已通过真实登录/改密流程轮换。另建独立 reader 和仅有 MCP invoke 权限的拒绝对照账号，未使用伪造 principal。
- 本地操作脚本保留在会话 `files/rag-local-acceptance/`。它复用现有 `scripts/run-backend.sh` 与 `npm run start --workspace=@repo/server`；未使用固定仓库 PID 目录的共享 `lens-local.sh restart`，避免干扰原实例，也不读取原实例 `.secrets.env`。
- 进程已脱离本次聊天保留运行；没有安装 systemd、开机自启、cron 或集群 Job。数据持久化不等于主机重启后自动拉起服务。

**真实验收**

1. 三组固定输入已真实导入，当前仍指向原批准的 Prism 日期及两个 Git commit；实验、测量、段落、图片和关联计数与第 3/4 步一致。
2. 重启前后均通过 Node 的真实 HTTP 链路执行固定公开基线，**20/20 案例、27 条独立核验引用、3 个数值封装**；覆盖九类问题。报告：[重启后公开基线](../../../.local/state/llm-d-lens-rag-acceptance/reports/live-public-baseline-after-restart.json)。
3. 另外 **7/7** 真实链路检查通过：全局管理员默认允许、显式全局授权 reader 允许、feature grant 撤销立即拒绝、匿名拒绝、CSRF 缺失拒绝、MCP 实际签名调用者结果与 REST 一致、仅 invoke 无 RAG 权限拒绝等。曾暂时移走一份独立归档证据，REST 返回 503、MCP 返回 `isError`，原字节恢复后查询恢复，未把故障伪装为空成功。
4. 真实管理 **6/6** 检查通过：未审批 origin 注册失败；periodic/commit 同版本幂等；错误事件 commit 和错误计数失败且保留旧 head；临时发布并查询实际上游父提交 `llm-d-benchmark` 文档，随后 CAS 回滚到初始 commit；旧字节保留；过期计划不能覆盖更新或回滚。[管理验收报告](../../../.local/state/llm-d-lens-rag-acceptance/reports/live-operations.json)保留命令退出码和结构化结果。
5. RAG 专用逻辑备份校验通过；恢复到独立 `rag_restore_verify` schema 和独立证据根目录，**首次插入 14,961 行，重复插入 0 行、更新来源 0 个**；三组 head、计数和原始字节哈希相符。行数包括注册、历史版本和清单，不代表独立实验数。[备份包](../../../.local/state/llm-d-lens-rag-acceptance/backups/three-source-rag.zip)不包含应用用户或 PostgreSQL 全库，不代表真实跨集群迁移完成。
6. 依次停止并重启仅本轮的 PostgreSQL、后端及 Node，PostgreSQL system identifier、数据库/数据目录、三组 head 与 generation、原库/恢复库行数及账号均保持；随后真实 REST/MCP 再过。见[持久化报告](../../../.local/state/llm-d-lens-rag-acceptance/reports/persistence-after.json)。
7. 内部 opt-in 不伪造历史记录；`agentic_benchmark_records` 与 `simulation_tasks` 在此实例均为 0。原先内部 12/12 属于隔离归属 fixture 回归，不是实际历史入库验收；6a 没有恢复执行。

**验收暴露并修复的问题**

- 响应模型继承了请求的 `str_strip_whitespace=True`，会将来源单元格 `" 30.3 s "` 改成 `"30.3 s"`，但 `subvalue` 仍为原始 `1:5`；原始上下文换行及 JSON 键也被裁剪。首次独立 HTTP 核验因此明确失败，没有调整期望来掩盖问题。
- 已将证据响应改为保留原始空白，仅 `QueryRequest`/`QueryFilters` 继续裁剪请求；增强公开基线核验器，禁止先裁剪原单元格再验证坐标。新回归修复前 **2 失败、1 通过**，修复后全过；受影响的契约、路由、评估器、SQL 查询、公开/内部基线和内部服务合计 **112 项通过，无跳过**。Ruff、格式、英文、空白及文档链接检查通过；没有声称完整静态类型检查通过。
- 用户 review 通过并明确要求提交后，此修复与公开中英说明已本地提交为 `c6f4796`；运行实例加载的内容与该提交一致，未推送。只读本地规划文档仍排除在提交之外。

**安装与后续边界**

- 对空库直接执行全历史 `alembic upgrade head` 时，在 `9c1f0a7b4d22` 因 `clusters` 不存在失败：历史 baseline 为 no-op，不创建原业务表。失败事务没有留下业务表；随后确认空库，复用现有安装器 Step 0 的 `apply_database_setup` 创建当前元数据 schema。
- 没有修改历史迁移，也没有用 stamp 伪装历史回放成功。本次证明的是仓库既有全新安装路径及本机 RAG 运行；全历史迁移链、正式生产、多 Pod 共享、远程/跨集群迁移、6a 和额外来源均未因此验收。
- **本轮已 review 并按要求提交；停在此处，下一阶段的共享/跨集群部署目标尚未选定，不自动开始 3a/3b/6a。**

以下章节继续保留原方案，描述目标设计、验收条件和完整实施顺序。

## 目标与边界

为三组公开证据构建可复现的问答索引：已下载的 Prism 快照、`llm-d` 指南及其已发布的基准测试结果，以及 `llm-d-benchmark` 文档。首个版本还通过一条独立的只读检索路径，搜索调用者有权访问的内部 Agentic 基准测试记录。系统回答关于已报告结果、实验条件和基准测试术语的事实性问题，不估算未报告的结果，也不推荐部署配置。

这是建立在现有 Lens PostgreSQL 数据库中的问答索引，不是 Agentic Deploy 规划器的扩展。导入公开来源不得写入 `agentic_benchmark_records`，也不得改变排序或 SLO 决策。第二条检索路径在调用者授权范围内读取该现有表，不将其中的行复制到公开索引中。规划所使用的独立证据效力与访问规则见 [Agentic Deploy 证据](./agentic-planning-evidence-and-rag.md)。

## 来源清单与证据效力

| 来源 | 导入内容 | 可支持的回答 |
| --- | --- | --- |
| 位于 `private/prism-public-2026-10-13/` 的本地 Prism 快照 | `historical-merged-deduplicated.json` 包含 249 行具有唯一 ID、符合归档结构的记录：177 条历史记录和 72 份规范化阶段报告。保留八份完整的公开运行载荷及其原始报告用于溯源；四份运行级摘要不算额外实验。 | 在核验来源、schema、单位和工作负载后，可作为结构化数值证据。 |
| `llm-d/guides/` | 指南 Markdown、20 份 Markdown 结果文档，以及 `benchmark-results/` 中五份自定义 JSON 结果文件。保留配置、各个表格单元格、叙述文字和实验链接。 | JSON 测量值和已发布的表格单元格可支持精确数值；叙述文字只能支持其明确表述的主张。 |
| `llm-d/guides/**/benchmark-results/*.png` | 37 张图片，关联到相关报告、图注和场景。记录已发布数据是覆盖整张图、部分数据系列，还是仅提供摘要。 | 仅提供趋势和视觉背景；图像像素不是精确测量值。 |
| `llm-d-benchmark/` | Markdown 文档，尤其是 `docs/` 和 `benchmark-report/README.md`；仅在解释结果需要时，索引实验和配置文档。 | 支持定义、方法、schema 和复现步骤；文档本身绝不构成测量结果。 |

Prism 快照保存在本地，并被 Git 忽略。其[快照 README](../../private/prism-public-2026-10-13/README.md) 说明了去重、排除项和单位。快照中的阶段时间戳是提交时间，不是实验时间；未知并发量保持为 null。该快照不包含独立配置的 GCS 或 GIQ 来源。`llm-d-benchmark` 的 v0.2.x 报告包含 `version`、`run`、`scenario` 和 `results`；五份指南 JSON 文件使用自定义格式，不得将其按 v0.2 报告解析。应保留报告声明的版本和指标单位，而不是假设所有来源都使用相同 schema。

## V1 证据模型

保持源文件字节不可变。新增一张 PostgreSQL 表 `rag_items`，使用类型化的行表示六类条目：

1. `source`：仓库、相对路径或 Prism 来源 ID、修订版本或快照日期、SHA-256、格式、访问分类，以及经核验的来源 URL。
2. `experiment`：来源范围内稳定的 ID、运行/阶段或指南/场景、模型、硬件、运行时、工作负载、配置、时间戳及其类型，以及指向原始来源的链接。缺失值保持为 null。
3. `measurement`：实验 ID、指标名、统计量（`mean`、`p50`、`p90`、`p99` 等）、数值、单位、已发布的数据系列/负载坐标、来源定位指针（JSON 路径或文档章节/表格/行/列），以及证据类型（`raw_json`、`published_table`）。在保存有文档依据的规范化数值/单位时，同时保留原始数值/单位。
4. `passage`：以章节为范围的 Markdown 文本或表格解读，带有标题路径及其在固定版本内的位置。
5. `figure`：路径、图注、关联场景，以及覆盖程度（`complete`、`partial`、`summary_only`、`unknown`）；经审查后，附上已知缺失数据系列的列表。
6. `figure_link`：从一个图条目指向一个实验条目的类型化关系，允许一张图关联多次运行，而不假设这些运行相同。

每行都包含条目 ID、类型、来源身份、固定修订版本/快照、内容哈希、访问分类，以及可选的自引用 `parent_id`。`figure_link` 行还包含被引用条目的 ID。经常用于筛选的字段保持为类型化字段，包括实验 ID、模型、硬件、指标、统计量、数值、单位和段落文本；来源特有的工作负载/配置、来源定位指针和覆盖详情使用 JSONB，但不能用 JSONB 代替数值筛选字段。对每个条目/版本使用稳定的来源范围键和唯一约束，实现幂等导入。查找键使用 B-tree 索引，段落文本使用 PostgreSQL `tsvector`/GIN 索引；不使用 SQLite 或 FTS5 索引。表约束和导入器校验必须限制每种类型允许的字段和链接。原始文档和基准测试报告仍保存在该索引之外，通过固定路径和哈希引用。

ID 的作用域是来源仓库及固定修订版本；文件名相同或模型相似，不能证明它们属于同一次运行。保留阶段与运行级摘要之间的关联，但不要将两者都计为独立测量。只有运行标识或可核验的来源元数据匹配时，才允许关联相应的指南实验、Prism 记录和 Agentic 基准测试，绝不能仅凭模型/硬件相似性关联。原始报告应始终可供审计。不要推断缺失的硬件、并发量、量化方式、请求形态或实验时间。文档位置，包括标题、字符偏移和 JSON 路径，只对其固定的来源版本有效；文件发生变化后应重新索引，而不是将过时偏移应用到新修订版本。

对于已发布的 Markdown 表格，应提取实际行/列单元格及其单位和数据系列标签；不要将整张表作为一条自由文本数值主张索引。样本数、测量窗口、单次运行的限制说明、失败请求和 SLO 定义等备注，应与相应实验一起保留。单位、统计量、分母或实验条件不同时，不得计算看似成立的性能提升。

## 检索与回答契约

公开语料使用现有 Lens PostgreSQL 数据库中的 `rag_items`，段落使用 PostgreSQL 全文检索。从固定来源重建这些行，不修改独立管理的 `agentic_benchmark_records` 表。只有评估表明全文检索遗漏了术语/改写类查询时，才可选用向量检索。这条仅用于回答的检索路径，不向规划器现有的 OpenSearch 或候选排序证据路径提供输入。

1. 在执行任一路查询前，先认证调用者并解析其权限。只有来源已被核验为公开、且调用者获准使用 RAG 功能时，公开语料才可读取。Agentic 分支仅能返回服务能够验证调用者有权查看其所属基准测试/评估上下文的记录；所属者缺失或无法核验时，省略该记录。应在查询中，或在任何候选离开该分支之前完成授权过滤，计数和摘要片段也在此范围内；绝不能先取出所有记录，再依赖语言模型过滤。
2. 将问题分类为数值查找/比较、方法或定义、图表/趋势问题；提取显式筛选条件，包括模型、硬件、指南、工作负载、负载水平、指标、统计量和单位。执行两条独立授权的检索分支：对 `rag_items` 进行结构化/全文检索，以及通过现有服务/DAO 契约对 `agentic_benchmark_records` 进行结构化检索。数值问题采用结构化条件筛选实验和测量；定义问题搜索段落；图表问题检索关联报告和覆盖状态。Agentic 分支只提供实测基准测试记录，绝不提供指南段落或从图片得到的数值。
3. 统一两个结果封装的展示格式，包括来源、来源范围内的实验/运行 ID、条件、指标/统计量/单位、证据类型、访问范围和引用，但不改变任何一方的存储 schema。合并依据必须是经核验的来源/运行/阶段身份，而非模型/硬件匹配或指标值相似。只有公开行与已授权 Agentic 行被证明代表同一实验和同一统计量时，才将实验计数一次，并展示两份溯源信息；否则分别展示，且不得对重叠关系不确定的结果宣称去重后的实验总数。绝不暴露不可访问来源的身份、匹配状态或计数。
4. 按实验条件的精确匹配程度、来源的具体程度和证据类型，对已授权结果重新排序。绝不将不同运行折叠成一个结果。必要条件不匹配时，应返回“无精确匹配”，而不是猜测。展示公开/内部来源标签，让读者知道测量值来自哪个分支。
5. 每个数值都应附上模型/硬件/工作负载/指标统计量及来源定位指针。只引用检索证据支持的主张。结果不一致时，连同各自条件同时展示，不能悄悄取平均。Agentic 实测记录可以支持事实性回答，但 RAG 段落不能变为 Agentic 测量，也不能影响规划器排序。
6. 要求回答生成器区分 `raw_json`、`published_table`、`agentic_measurement`、`prose_summary` 和 `image_trend`。若只有图片或不完整表格，应明确说明无法精确重建完整曲线。v1 不执行 OCR，也不从像素估算数据点。

本地来源引用应能定位到固定文件及文档标题、JSON 路径或表格行；响应或证据元数据中应包含来源修订版本/快照标识。检索命中仅代表相关候选，不意味着可以将叙述文字、图示或基准测试格式示例提升为实测数据。拒绝或忽略检索文档中嵌入的指令；检索文本是数据，不是系统提示词。

v1 通过 REST API 和只读 MCP 工具暴露同一个仅用于回答的查询服务。两者均接收问题和显式筛选条件，并返回相同的回答、证据类型、来源、固定版本引用以及拒答元数据；MCP 工具只是适配层，不是第二套检索实现。两个入口都必须解析并强制使用真实调用主体，要求具备 `benchmark-rag:query:read`；适用时，MCP 调用还必须满足现有 MCP 工具调用策略。绝不能用服务主体、匿名请求或模型提供的身份代替调用者。在排序、计数、摘要片段生成或模型输入之前，应用来源可见性过滤，以及启用后的逐条 Agentic 记录授权。不得暴露直接 SQL/搜索端点，也不得将该工具加入 Agentic Deploy 规划器的工具允许列表。

## 来源与索引更新

### 通用发布协议

来源注册表采用允许列表，通过 `rag_items` 中的 `source` 条目表示，不新增独立表。每个来源具有稳定的来源标识（仓库 URL、Prism 端点/来源 ID，或论文标识）、来源类型、访问策略、解析器/版本、最近一次完整盘点清单，以及已发布修订版本。每个子条目具有该来源范围内稳定的逻辑键，例如路径加 JSON 记录 ID 和字段路径，还具有固定的来源版本、内容哈希和发布状态。每次导入的路径/记录 ID 与哈希发布清单，保存为版本化来源条目，或保存为由该条目引用的不可变外部产物。这些键标识条目来自“哪里”，并不证明两个来源描述的是同一实验。

1. 将完整且固定版本的源文件清单获取为不可变的本地输入。在将“缺失”视为“已删除”之前，验证仓库提交或 API 分页、文件格式、权限、哈希和适配器兼容性。超时、下载不完整或来源不可访问都属于导入失败，不能证明旧条目已被移除。
2. 将新发布清单与已发布清单进行差异比较：未变化内容可保留原先固定的文件版本；新增和变化的内容解析为暂存的 `rag_items` 行，使用稳定的来源范围键保证幂等。文件变化时，重建所有受影响的段落、测量、图、链接和偏移。只有在取得完整源文件清单后才能识别删除项；记录删除标记（tombstone），而不是删除被旧引用使用的版本。
3. 发布前验证来源计数、各类型计数、引用链接、单位、提取错误、受影响图的覆盖程度、来源哈希，以及回答回归测试。新解析器或 schema 修订需要兼容性检查；schema 变化还需要经过审查的 Alembic 迁移。不得将仅部分解析成功的测量作为精确证据发布。
4. 在 PostgreSQL 中原子切换来源条目的已发布清单指针；发布清单列出该版本可见的确切版本化条目 ID，包括从较早文件版本保留的未变化条目。检索只依据该清单的成员关系和有效链接，不依据可能暴露混合版本的独立逐行 `active` 标记。对于跨来源关系，重新验证任一侧受影响的链接，并同时发布两个受影响版本；或者在验证之前保持链接未解析。验证或事务失败时，上一版本仍应可检索；回滚恢复其固定发布清单，而不是混合新旧行。切换指针时，对同一来源的并发刷新进行串行化。
5. 按明确定义的保留策略保留被替代的条目和源文件字节，以保证历史引用仍可验证。只有满足保留策略并通过依赖检查后才能删除；不得覆盖固定内容，也不得将某个来源范围键复用于无关实验。独立授权的 Agentic 分支始终实时读取 `agentic_benchmark_records`；任何公开来源刷新都不得复制或删除其中的行。

v1 对新下载的 Prism 快照执行手动刷新；两个允许列表中的仓库，在导入器存在后采用提交触发或定期刷新。记录触发方式、导入状态、来源修订版本、新旧计数、失败信息和激活时间，但不记录凭据。初始 249 行 Prism 记录及当前指南文件数量只是基线检查，不是永久固定的预期总数。无变化刷新必须幂等。

### 更新场景

| 变化 | 导入与发布规则 |
| --- | --- |
| `llm-d` 或 `llm-d-benchmark` 新增文件或基准测试结果 | 比较固定提交的完整允许列表文件清单与已发布清单。只解析受支持的新文件及其依赖；新增指南测量必须同时具备模型、场景、单位和表格/JSON 定位指针。暂存受影响的图链接，并在验证后发布。除非来源元数据证明相同，否则新的实验 ID 始终保持独立。 |
| 任一仓库删除、移动或重命名文件 | 用完整源文件清单确认删除；将旧路径及其依赖条目退出默认搜索，但保留固定的上一版本用于引用。只有 Git 历史或内容身份支持时，才将移动视为一个与旧来源关联的新路径；更新或失效化受影响的图链接和跨文档链接。绝不能根据获取失败或不完整检出推断删除。 |
| Markdown 文档或结果表格变化 | 重新解析变化文件，即使只有标题或布局发生移动；将段落、全文检索向量、偏移、表格单元格测量和出链作为一个暂存单元整体替换。旧引用继续指向旧提交/哈希。核对变化数值和备注与相应实验是否一致；不能让过时段落或表格行引用新修订版本。 |
| 提议新增仓库或数据集 | 要求明确的来源批准，包括所属者、公开可见性/许可证、允许路径/格式、检索权限、保留策略和更新触发方式；要求固定的初始版本、必要时的专用适配器，以及代表性的解析/回答测试。以新的来源标识注册；不得悄悄爬取任意仓库，也不得将存在重叠的实验视为相同。对于权利受限的数据集，在访问路径和过滤方案设计完成前，不得建立公开索引。 |
| 启用新的 Prism 来源，包括 GCS/GIQ | 核验端点/存储桶授权及公开状态，枚举每个可访问页面/对象及其稳定 ID/版本，只获取获准的原始报告。验证报告 schema、时间戳类型、单位，以及与现有归档/Results Store ID 的重叠。发布独立的来源版本，记录保持在该来源范围内；对已证明重复的记录建立关联，避免重复计数。分页不完整或授权失败时保留上一已发布版本。GCS/GIQ 导入属于 v1 之后的扩展，不在初始 249 行导入范围内。 |
| 新增论文或其他研究文献 | 先核验发表身份（DOI/arXiv ID 加版本）、文本索引权利、撤稿/撤回状态，以及检索范围。在固定版本保存元数据、摘要或获准全文段落、页码/章节定位及引用溯源；PDF 或 HTML 变化后重新提取。论文主张默认仅用于解释，不属于 Prism/指南测量或 Agentic 记录。只有专用适配器验证实验条件、统计量、单位和许可证后，才可将已发表数值表格提升为测量证据；绝不能从图中推导精确数值。撤回时，将当前来源退出检索并保留审计删除标记。这同样属于 v1 之后的扩展。 |

为新增、删除、重命名、标题/表格变化、不完整清单、解析器失败、重复导入、跨来源链接失效、来源回滚、权限变化和论文撤回添加场景测试。访问权变化必须在下一次回答前生效，即使内容刷新尚未发生；当前授权无法核验时，默认拒绝访问。必须在全文排序、摘要片段生成、计数和跨分支合并之前，应用已发布来源版本过滤和权限检查。

## 实施顺序

### 0. 固定输入并确定访问边界

记录 Prism 快照日期与文件哈希、`llm-d` 和 `llm-d-benchmark` 的确切提交、允许的路径/格式，以及保留与引用要求。确认 RAG 功能权限，以及现有所有权模型能否安全暴露任何 Agentic 记录；没有可核验所属者的基准测试 ID 不符合条件。准备固定问题集，涵盖精确指标、指南表格、定义、图表缺失点、文件变化/移除、跨来源重叠，以及已授权/未授权内部记录。**验收门槛：**公开来源清单与访问决策已形成文档；被 Git 忽略的输入不进入 Git；每个问题都有已知证据与预期拒答情况。

#### 第 0 步验收记录（2026-10-14）

初始固定输入为日期为 2026-10-13 的本地 Prism 快照、提交 `080c14d957755da4cc363745638619fc748558f1` 的 `llm-d`（检出仓库的 origin 为 `https://github.com/VincyZhang/llm-d`），以及提交 `6e081de16d2debc7b29436ad53f2c62e8d475f2e` 的 `llm-d-benchmark`（检出仓库的 origin 为 `https://github.com/llm-d/llm-d-benchmark.git`）。固定时，两个仓库工作区均干净。这些信息标识的是检出来源，并不意味着其中链接的每份文档或外部 URL 都是公开的；导入器暴露任何条目前，必须核验来源的发布权利。

当前 llm-d 来源已改为官方 `https://github.com/llm-d/llm-d`。仓库刷新省略
`--source` 时，会从官方 GitHub 获取审核过的完整 commit，存入共享托管仓库缓存。
仍支持显式离线仓库，但必须核对 origin 和固定 commit；Prism 快照仍需本地来源。
原初始 commit 同样存在于官方仓库，139 份文件的内容清单不变；包含来源身份的清单
哈希更新为 `5cc884dcde2bf7986f86c7ed200a8293fd60623d1f7c2049b10daa6b24c46aee`。
历史 fork 来源的记录、引用和审计快照保持不变，必须按官方来源重新审核导入。

v1 的允许列表为 `llm-d/guides/**/*.md`（指南文本，包含 20 份结果 Markdown 文件）、`llm-d/guides/**/benchmark-results/**/*.json`（五份自定义结果文件），以及 `llm-d/guides/**/benchmark-results/**/*.png`（37 张图，仅用于背景说明）。在 `llm-d-benchmark` 中，允许 `docs/**/*.md`（27 个文件）和 `benchmark-report/README.md`，用于定义与报告格式背景，而非实测数据；`benchmark-report/llmd_benchmark_report/br_v0_2_json_schema.json` 仅用于验证 v0.2 报告格式。其他 JSON、图片、指南配置资产或仓库路径均未被隐式批准。清单以以上提交为准，不是永久固定的预期数量。五份指南 JSON 位于 `guides/p2p-kv-cache-sharing/benchmark-results/data/glm-5.2-c64/`；其格式不是 benchmark-report schema。符号链接、子模块和外部链接不会自动成为额外导入根目录。

本地输入根目录为本仓库中的 `private/prism-public-2026-10-13/`，由 `.gitignore` 的 `private/` 规则排除。本次导入仅允许其中两份归档 JSON、两份 Results Store 索引 JSON、八份完整运行 JSON 载荷、八份原始 ZIP 导出、规范化规则和合并 JSON。其 README 记录来源端点和排除项。以下 SHA-256 针对已存储字节计算，路径相对于该根目录；README 仅用于说明，不是导入输入。

```text
6b41f1175789429620f51e3be6c7545d94c16c4b6b25c2ddd286406159ff4fee  archives/archived_drive_data.json
1ae3cd3945059dfd7a4eaf15d99f680242a2e124eaaad2238fa1ff4abb731bd6  archives/llm-d-benchmarks.json
6ff00bf899c77f1b656160082dc406538d3781fb842bb6f6e2caf63426ef9e0e  historical-merged-deduplicated.json
aec36a726fc140924589271859985ad92d06cf623eade8305b72223589c860e6  normalize-and-merge.jq
36e96467c27235c84591d36f0011373736ec85395fdfc926759317683133f74e  results/promoted-index.json
b03f1b6351e8e270d44fdaf793fef36f2b99f97bec093bc1bd308832f2bdbea2  results/public-index.json
a9c6b0b610514e44670a9eb9287a4371675a262528902697e1f02921f850b07f  results/json/18865f26-7574-4828-9c44-b08b20d34be9.json
f76dd9055619b076859445bbcf5de304dd1d9dbd0d8600f2caf60f25b2d8c126  results/json/5450510e-7af5-484b-a06a-89af5eab5265.json
f31add5affb5c40547d29756bc34c7b7abc9dca8d735b0d35ad53149c6f14615  results/json/63a6adee-542c-4817-9c83-32c3fc007a63.json
edfd4e20db8e4ea383741e2191719dccf70cb7fe6b44dc14eaab181760048f52  results/json/a8f1b438-49ca-482b-bd3a-5303defd33a1.json
f7fa15f21aae70e8e96a66763915d91067965066fbfc914bfc1d62fb38d87d13  results/json/c2602342-6fc1-4b6b-ab1e-8e9a12364697.json
55ebe0791ea7d11e9b93ca1b9e2198b66ff037c6b5f5b3e4a11d7b5a74c56a65  results/json/cdf3e25a-0265-4414-98e8-dbb7dc594d55.json
f96ea3cd73662999979b2404375206b73d35e259e3f8a3f61f2871617397bbf6  results/json/ced8634e-33ff-463a-b539-425594891f2d.json
144f310c2c2cf1d48e5c07e0364c0e6cde8e3ab222f2026d27faab7838a894f6  results/json/e354540c-ed82-4442-8405-7e5b0327c581.json
ee8a97b5e0290849c681fbddbfd3066f8455483bb17e1d2785a9db59bc576b5d  results/zip/18865f26-7574-4828-9c44-b08b20d34be9.zip
175f72dca6542baa6434638fe34851a70be76bf956ec491fbb2aeff4d4386948  results/zip/5450510e-7af5-484b-a06a-89af5eab5265.zip
f1cf77adc94a3431db279c1c6f80c0b74004131a2e8378a9a8c8d4738f7da886  results/zip/63a6adee-542c-4817-9c83-32c3fc007a63.zip
eff0af959b4d3ebe17aeb32e344ff5d6f11bc445162c83c75c587910c49e13dd  results/zip/a8f1b438-49ca-482b-bd3a-5303defd33a1.zip
3091c4d0e9381fe587e14e5559f42e35edf78398baaf2e26797880723aeb174f  results/zip/c2602342-6fc1-4b6b-ab1e-8e9a12364697.zip
1c78aebde0dc5a41c659578a4d0739e789fd10e930d70bfdcbf49045576b89b0  results/zip/cdf3e25a-0265-4414-98e8-dbb7dc594d55.zip
2de148c8fff89fee1f23be4f5ec3cd3184ec1a83a5525e378b91f3c533c5d072  results/zip/ced8634e-33ff-463a-b539-425594891f2d.zip
e3468f6bfa6ed565e7ea4538a7fee0439986d8e0b5029399cfd89f0977959903  results/zip/e354540c-ed82-4442-8405-7e5b0327c581.zip
```

v1 访问决策：启用任何查询端点前，引入专用权限 `benchmark-rag:query:read`；公开分支要求调用主体已认证并具备该权限，不得通过 `evaluate:run:read`、`candidate:candidate:search` 或匿名访问隐式授予。角色分配和端点强制执行属于第 5 步。公开索引条目仅限经核验的公开来源；权限撤销必须在查询时生效。当前权限目录尚无 RAG 权限。Agentic 分支在第 6 步前保持禁用：`BenchmarkRecordDao.list()` 未限定访问范围，`evaluate_run_id` 是可为空的软引用，而 `evaluate_runs` 的所属者可能为空，或者其行已被删除。在返回任何命中、计数或摘要片段前，第 6 步必须基于已认证主体和 `evaluate:run:read`，核验一个仍然存在且具有所属者的评估上下文。主体、所属者或有效上下文缺失时，该分支必须默认拒绝；现有 `resource_readable(None, ...)` 快捷逻辑不适用。任何 Agentic 记录都不复制到公开索引中。

引用策略：每次回答携带仓库身份、提交、相对路径、文件哈希，以及标题/表格单元格或 JSON 路径；Prism 还需携带快照日期和原始运行/报告路径。解引用固定文件前，核验其字节。条目移除后，将已退役条目及其固定源文件字节至少保留 365 天；若任何保留引用或依赖条目仍需要它们，则继续保留，取两者中更长者。此后的任何删除都需依赖检查和经审查的保留决策；绝不能用刷新版本覆盖历史源文件字节。本地快照 ZIP 和 JSON 继续被 Git 忽略，本文不发布这些文件。

固定验收问题如下（以下答案表示预期证据，不是生成输出）：

| 问题 | 预期证据或拒答 |
| --- | --- |
| Prism ID `02c8f038-1cad-4ee8-a72e-724affa923f4` 记录的吞吐量是多少？ | `historical-merged-deduplicated.json` 中该 ID 所在行的 `metrics.throughput`：`6131.06177725188` output tokens/s；同时提供模型 `Qwen3-0.6B` 和来源快照。不要将 `metrics.output_tput` 作为第二次观测。 |
| H200 gpt-oss-120b 指南观测中，P2P 与默认加载器的权重加载时间分别是多少？ | `guides/modelexpress-p2p/benchmark-results/h200-ib-gpt-oss-120b.md` 的 Weight loading 表格：`0.93 s` 对比 `30.3 s`，并附上报告中的跨节点/已预热 NFS 条件与限制说明。 |
| 基准测试报告旨在记录什么？ | `llm-d-benchmark/docs/benchmark_report.md`：记录某个工作负载/环境的聚合性能统计，而非逐请求追踪；schema 示例中的任何值都不是测量结果。 |
| 能否列出 Qwen3 Coder TPU 延迟-吞吐量 PNG 中未报告数据点的精确数值？ | `llm-d/guides/agentic-serving/qwen3-coder-480b-tpu.md` 链接到 `benchmark-results/latency_vs_throughput.png`；没有已发布的底层数据时，拒绝给出精确数据点，引用指南和图片，并说明经审查的图覆盖程度。 |
| 在完成一次固定版本的完整刷新前，新增或移除的指南报告会反映到检索结果中吗？ | 不会；在新的完整清单通过验证并原子发布之前，检索继续使用旧的已发布清单。不完整列表不能证明删除。 |
| Prism 与指南中模型/硬件相似的行是否代表同一实验？ | 未核验运行/阶段或来源身份时，不合并；对于重叠关系不确定的结果，不提供去重后的实验计数。 |
| 具有 `benchmark-rag:query:read` 的已认证用户，能否看到属于其可访问评估的基准测试？ | 只有第 6 步证明该评估仍存在且具有所属者、调用者被授予 `evaluate:run:read`，并且基准测试 ID 与该评估关联后才可以；在此之前不返回内部记录。 |
| 其他无权访问的用户、未认证调用者，或所属者缺失/已删除/无法核验的记录，能否出现在结果或计数中？ | 不能；省略该记录、身份、任何重叠信号及其计数。调用者没有 RAG 功能权限时，拒绝整个查询。 |
| 同一主体使用相同筛选条件调用 REST 和 MCP，会得到相同证据吗？ | 会；比较引用、来源标签、拒答信息和已授权计数。缺少 `benchmark-rag:query:read` 时，两者均拒绝；MCP 调用还须满足其自身工具策略。在第 6 步通过前，两个传输入口都不能暴露 Agentic 分支。 |

### 1. 创建单表契约与迁移

新增 `rag_items` SQLAlchemy 模型和 DAO，并提供针对 PostgreSQL、经过审查的 Alembic 迁移。明确六种条目类型、稳定的来源范围键、固定修订版本/哈希、类型化数值字段、自链接、发布状态、来源发布清单成员关系、约束、B-tree 筛选索引和 `tsvector`/GIN 段落检索。定义单位/统计量映射，以及仅允许公开来源的访问分类；原始文件单独保留。**验收门槛：**迁移和 schema/DAO 测试在 PostgreSQL 上通过；同版本导入幂等；不虚构未知字段/单位；模型和导入器都不写入 `agentic_benchmark_records`。

### 2. 在首次导入前实现发布机制

实现允许列表来源注册表、完整源文件清单校验、暂存、版本化条目 ID 发布清单、来源指针切换、删除标记和回滚。对同一来源的竞争导入进行串行化。在全文排序或计数前，通过已发布清单过滤查询；新清单可继续引用未变化条目的旧固定文件版本。不完整列表中缺失的文件不得被退役。**验收门槛：**激活之前或期间失败时，旧版本仍可检索；并发刷新不混合版本；删除项退出当前搜索，但历史引用仍可核验；未变化刷新不产生重复条目。

### 3. 导入初始 Prism 快照

使用现有规范化归档适配器处理 249 行记录，包括 177 条历史记录和 72 份阶段报告，保留原始 JSON/报告路径与哈希。展开已知指标叶子字段，不因别名重复计数；将四份运行级摘要作为上下文关联，而非额外实验。保留提交时间戳类型、null 并发量、实测速率与目标速率的区别，以及有文档依据的时间/吞吐量单位。**验收门槛：**初始已发布清单精确解析出 249 行来源范围内的实验记录；第二次导入不改变计数；抽样数值能追溯到原始 JSON 路径；未来刷新不得将 249 硬编码为永久总数。

### 4. 导入指南结果与基准测试文档

解析指南标题、备注、配置、表格、图注和链接；为五份自定义 JSON 文件使用独立适配器。表格单元格测量只能关联到已核验场景，而非仅匹配模型。盘点 37 张图，记录其 `complete`/`partial`/`summary_only`/`unknown` 覆盖程度，不从像素提取数值。按版本化章节索引 `llm-d-benchmark` 文档，将报告示例明确标为示例而非测量。**验收门槛：**计数匹配固定版本清单；表格/JSON 数值能往返定位到原始位置；GLM 数据不能关联到 P2P GPT-OSS 图；定义查询绝不将示例数据作为真实测量返回。

### 5. 交付仅公开来源检索与回答校验

在已发布的 `rag_items` 上新增类型化数值筛选、PostgreSQL 段落检索、来源版本/权限过滤、引用，以及区分证据类型的响应。解引用固定原件前核验来源哈希。缺少比较依据或图中数据点未报告时拒答；语言模型不可用时，保留确定性的证据响应。交付由同一个调用者范围服务支持的 REST 查询端点和只读 MCP 查询工具，共享问题/筛选输入与证据/回答输出契约。将 `benchmark-rag:query:read` 加入权限目录，并在两个入口都强制要求；不得仅凭 `mcp:tool:invoke` 推断 RAG 访问权。**验收门槛：**公开问题集只返回匹配实验及固定来源引用；排除未激活条目；绝不将叙述文字或图片转换为测量；相同主体和筛选条件下 REST 与 MCP 一致；拒绝未授权调用者；保持仅公开来源访问。这条可用的公开检索路径无需等待 Agentic 授权工作。

### 6. 设置授权门槛并加入 Agentic 检索分支

先建立限定调用者范围的授权查询，将 `agentic_benchmark_records` 关联到调用者可访问的所属上下文。当前 DAO 的 `list()` 未限定访问范围，而 `evaluate_run_id` 是软引用，因此二者都不足以直接支持面向用户的检索；所属者缺失、已删除或无法核验的记录必须省略。只有该边界测试通过后，才向共享查询服务添加独立的只读 Agentic 分支，并基于已核验的运行/阶段及指标身份合并已授权结果封装。不将公开条目复制到 Agentic，也不改变规划排序器。**验收门槛：**REST 和 MCP 返回相同的调用者已授权证据；不同主体只接收各自被允许的记录；未授权匹配不通过计数/摘要片段/重叠状态泄露；不确定重叠保持分离；已核验重复项仅计数一次并保留两份引用。若此门槛未通过，两个接口都交付仅公开来源路径，Agentic 分支保持禁用。

### 7. 验证刷新场景并启用触发机制

测试每个允许列表仓库中的新增、删除、重命名和内容/标题/表格修改；测试新 Prism 快照、解析器错误、API 分页截断、并发运行、访问权变化和原子回滚。之后启用 Prism 手动刷新，以及经审查的提交触发或定期仓库刷新，并报告导入状态和计数。**验收门槛：**新增数据带有固定版本引用；移除数据只有在取得完整清单后才退出检索；旧引用仍可解析；更新失败保留上一版本；访问权撤销在下一次回答前生效。

### 8. 评估并单独接入额外来源

对三组公开来源回放固定回答集；若第 6 步通过，还涵盖已授权 Agentic 记录。检查无依据数值、错误关联、引用失败和访问泄露。只有基线通过后，才通过独立的来源批准、权限/许可证检查、适配器、发布清单和回归测试夹具，接入其他仓库/数据集、GCS/GIQ Prism 来源或论文集合。**验收门槛：**每个发布数值都可追溯到获准的原始证据；论文在未独立验证前只用于解释；撤回的来源不再出现在当前检索结果中；新来源不会仅因发现其 URL 就变为可检索。

## V1 明确不包含的事项

- 不从 PNG 提取数值，也不重建未报告的图表数据系列。
- 没有来源层面的身份依据时，不对不同来源 ID 进行语义去重。
- 不导入额外的 Prism GCS/GIQ 来源或私有/用户基准测试。
- 不写入 Agentic Deploy 规划记录，也不改变其排序契约；只读 Agentic 查询必须执行调用者现有授权，绝不能绕过授权。
- 未检查实验条件前，不宣称归档行、阶段报告和指南结果可以直接比较。
