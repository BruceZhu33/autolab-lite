# LABNOTE（autolab-kb）：个人科研知识库 设计与开发文档 v2 · 实验室生产版

日期：2026-10-13
状态：提案（待开工）
版本：**v2，取代同日 v1（个人单机轻量版）**。变更原因：部署目标从"个人笔记本常驻"升级为"实验室服务器、约 10 人、多用户、真实生产"，v1 的 SQLite 单文件选型按单人场景校准，不再适用；架构基座改为 **SuperMew-main/backend/ 全栈保留 + 生产加固**。
新仓库建立后本文档随迁；与 `docs/PRODUCT-DIRECTION-2026-09-28.md` 方向二（LABNOTE for agents）的呼应关系不变。

---

## 1. 背景与目标

科研工作者的三条使用链路（与 v1 相同）：

- **链路 A（记录入库）**：读论文后写笔记，论文 PDF + 笔记自动进入知识库，按"知识空间"组织；
- **链路 B（专属 Agent 对话）**：与知识库对话，提及相似概念时检索出相关论文片段/笔记并给出引用；
- **链路 C（自动迭代供知识）**：autolab-lite 研究迭代时选择知识空间 + 引导想法，研究 Agent 每轮利用知识库检索自行改码-实验-分析，实验结论回流知识库。

v2 新增的生产性目标：

- **多用户**：实验室约 10 人，认证 + 按空间隔离（共享空间 + 个人私有空间）；
- **生产部署**：实验室服务器上的 docker compose 栈、TLS、备份与恢复演练；
- **不是玩具**：并发正确、数据可备份可恢复、权限可审计。

非目标：跨实验室 SaaS 化、海量租户、高可用集群、Milvus 分布式模式、复杂组织架构权限（两级角色足够）。

## 2. 总体决策与可行性

**核心决策：以 SuperMew-main/backend/ 为生产基座，保留其全部基础设施（Milvus+etcd+minio+Postgres+Redis+Attu 六容器）与检索主链路，在其上做四类工程：P0 缺陷修复、多租户改造、摄入升级、部署运维化。**

可行性判断：

1. **基础设施在服务器场景下全部合理**。v1 反对 6 容器的唯一理由是笔记本常驻成本，服务器上不成立；10 人/10³–10⁴ 文档量级下 Milvus standalone + 单 Postgres + 单 Redis 绰绰有余，不需要任何分布式组件。
2. **必须直面的判断：保留基础设施 ≠ 生产就绪**。前期对 SuperMew 的代码分析确认了一批单用户无感、多用户即事故的缺陷（全局状态串台、过滤表达式注入、检索路径解析不一致、BM25 状态文件竞态、零多租户），全部列入 §5–§6 定向修复，无架构级返工。
3. **摄入与评估沿用 v1 结论**：结构感知分块替代字符按页切分；gold 集评估先于一切调参。
4. **链路 C 依赖不变**：等 autolab `refactor/stage-agents` 合并 main 且真实 GLM 跑通；本项目通过 HTTP 契约对接，不引入对 autolab 的 import 依赖。

架构总图：

```text
 实验室成员 ──HTTPS──▶ Caddy(443, TLS)
                       ├─ /            ▶ labnote-api (FastAPI, 容器化, 自带认证)
                       │                  ├─ 摄入: 解析(pymupdf4llm/md) → 结构分块(三级树) → bge-m3 → Milvus
                       │                  ├─ 检索: hybrid(dense+BM25 sparse) → RRF → auto-merging → rerank
                       │                  ├─ 会话: LangChain agent + 工具(search_knowledge), SSE
                       │                  └─ 关系数据: users/spaces/members/documents/parent_chunks/会话/BM25统计
                       │                  ├─ Postgres ◀── 全部关系数据(含 BM25 统计, 取代 JSON 文件)
                       │                  ├─ Redis    ◀── 会话/父块缓存
                       │                  └─ Milvus(+etcd+minio) ◀── 稠密+稀疏向量, partition_key=space_id
                       └─ /attu (仅 VPN/SSH 隧道可达, 不对公网)
 autolab-lite(链路C, 后期) ──HTTP PAT──▶ /api/v1/retrieve · /api/v1/ingest(回流)
```

## 3. 代码基座手术（从 SuperMew 到 labnote）

新建仓库（建议 `/Users/juan/Documents/autolab/autolab-kb/`，服务器上 clone 部署），从 SuperMew-main 复制并整理：

| 动作 | 内容 |
|---|---|
| **保留并改造** | `backend/` 全部（api/agent/rag_pipeline/rag_utils/milvus_client/milvus_writer/embedding/parent_chunk_store/document_loader/cache/database/models/auth/tools/upload_jobs/schemas）；`frontend/`（加登录页与空间切换）；`docker-compose.yml`（加 api 与 caddy 两个服务，见 §10） |
| **删除** | `langchain-study/`（学习笔记）、`app/`（复现代码，含其危险的哈希 embedding 与不清理向量的上传路径）、根目录 `test_embedding.py`、`volumes/`（数据目录进 .gitignore，绝不入库） |
| **重写** | `test_langsmith_eval.py` → §9 评估体系（检索级指标为主，LangSmith 降为可选） |
| **改名/配置** | 包名 supermew→labnote；`.env.example` 增补：JWT 密钥、空间默认配额、嵌入模型路径；凭据全部走环境变量/secret 文件 |

依赖版本随 SuperMew 现状（Milvus 2.5.14 / pymilvus、langchain v1 系、Python 3.12），不在 L0 引入大版本升级。

## 4. 多租户数据模型

**先行设计参考**：advanced-rag-main 已有可用的知识空间实现（`routers/knowledge_spaces.py` + `services/rag_service.py:69-121`）。本设计直接借鉴其：空间 CRUD API 形态（分页列表、casefold 名称去重撞名 409）、`is_default` 默认空间兜底（未选空间时回落）、`kb_<slug>_<uuid8>` 式稳定命名。其隔离机制为**一空间一 Qdrant collection + 多空间并行检索后拼接**；本设计改用 **Milvus 单 collection + partition key**，取舍见 §4.2。其没有用户/成员/可见性概念（匿名系统），users/spaces_members 及私有空间为本设计新增。

### 4.1 用户与空间（Postgres 新表）

```sql
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,          -- argon2
  role TEXT NOT NULL DEFAULT 'member',  -- admin | member
  disabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE spaces (
  id TEXT PRIMARY KEY,                  -- 短标识, 如 "llm-efficiency"
  name TEXT UNIQUE NOT NULL,
  name_key TEXT UNIQUE NOT NULL,        -- casefold 去重(借自 advanced-rag)
  description TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'shared',  -- shared(全员可读) | private(仅成员)
  is_default BOOLEAN NOT NULL DEFAULT FALSE,  -- 默认共享空间兜底(借自 advanced-rag)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 注意: 无 per-space embedding 字段——单 collection + partition key 意味着
-- embedding 模型/维度是部署级锁定(环境变量), 换模型 = 重建 collection 全量重嵌入。

CREATE TABLE space_members (
  space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'reader',  -- owner | editor | reader
  PRIMARY KEY (space_id, user_id)
);
-- 语义: reader=检索+对话; editor=+上传/删自己的文档; owner=+成员管理/删任意文档/改空间设置
```

认证：用户名密码 + 签名会话 token（PyJWT，短期有效），密码 argon2 哈希；admin 用 CLI 建号（`labnote user create`）。实验室 SSO 明确不做。

### 4.2 空间隔离贯穿三个存储

- **Postgres**：`documents`、`parent_chunks`、会话表全部加 `space_id` 列 + 索引；文档归属 `uploaded_by`。
- **Milvus**：新建 collection `labnote_chunks_v2`，schema 在 SuperMew 基础上加 `space_id VARCHAR` 并声明为 **partition key**（Milvus 2.5 原生支持，默认 64 分区够用）。检索一律带 `space_id in [...]` 过滤 → 分区裁剪，天然隔离与提速。旧 `embeddings_collection` 数据为测试数据，不迁移，新库冷启动。
- **为什么不用 advanced-rag 的"一空间一 collection"**：collection-per-space 的优点是每空间可独立换 embedding 模型、删空间即删 collection（硬隔离）；代价是**跨空间检索必须并行扇出再拼接**（advanced-rag 即如此，`asyncio.gather` 后 extend，结果顺序带空间偏置），且 N 份 HNSW 索引/备份面随空间数线性增长。实验室的高频场景是"个人私有空间 + 实验室共享空间"同查——单 collection + partition key 下这是一次 `hybrid_search`、RRF 在合并候选上统一融合，一次备份一个 collection。embedding 全库统一（部署级锁定）在 10 人实验室是可接受约束。若未来确需按空间换模型，再切回 collection-per-space，数据模型无需变（`space_id` 字位保留）。
- **BM25 统计**（见 §6）：全部入 Postgres，按空间分账。

### 4.3 权限执行点

API 层统一鉴权中间件（token → user → 可见空间集合缓存于 Redis）；检索/列表/阅读/上传/删除在进入业务前校验空间角色；**Agent 工具不允许由 LLM 决定检索范围**——会话创建时绑定空间集合，工具内部隐式携带，防止提示词注入越权读他人私有空间。

## 5. P0 缺陷修复（上生产前必须完成）

来自对 SuperMew 代码的分析，全部是定向小改：

| # | 缺陷 | 位置（SuperMew backend/） | 修复方案 | 验收 |
|---|---|---|---|---|
| 1 | 模块级全局状态传 RAG 上下文/工具计数/步骤队列，并发串台 | `tools.py:15-18,21-64` | 改为 `contextvars.ContextVar` 或显式 Request 上下文对象贯穿传递；每轮计数器入会话状态 | 双用户交错对话的自动化测试，断言 trace 不串 |
| 2 | Milvus 过滤表达式字符串拼接，文件名含 `"` 可注入/破坏 | `api.py:219,443,498`、`milvus_client.py:181-182` | 统一 `build_filter()` 助手：白名单字段 + 引号/反斜杠转义，禁止各处手拼 | 文件名含 `"`、`\` 的上传/删除/检索用例通过 |
| 3 | hybrid 与 dense 路径 hit 解析不一致（其一取空文本） | `milvus_client.py:263-274 vs 310-318` | 移植复现代码已修好的双形态 `_format_hit`，两路共用 | 单测断言两路字段非空；构造 hybrid 失败降级 dense 返回非空 |
| 4 | `GRADE_MODEL` 默认 `gpt-4.1` 与 Ark 端点不符，评分节点异常未捕获 | `rag_pipeline.py:16,158-189` | 默认改为主模型同源；节点异常按"评分不可用→走改写"处理并记 trace | 不设 GRADE_MODEL 时端到端跑通 |
| 5 | 静默吞异常（cache 全部方法、`except: pass`） | `cache.py`、`api.py:224-231` | 至少 warning 日志 + trace 记录降级原因；`delete_pattern` 的 KEYS 改 SCAN | 日志可见 Redis 断连事件 |
| 6 | 重传文档的清理链路脆弱 | `api.py:210-283` | 与 §6 的 BM25 入库事务化一并重构，删除链路幂等可重试 | 重传同文件 N 次，doctor 校验统计一致 |

## 6. BM25 状态从 JSON 文件迁入 Postgres（按空间分账）

SuperMew 的 `bm25_state.json`（vocab/df/N 的读-改-写文件）是全项目最 fragile 的部分：多用户并发摄入直接竞态丢更新，且该文件在备份集之外即废掉全部稀疏向量。v2 方案：

```sql
CREATE TABLE bm25_vocab (
  idx INTEGER PRIMARY KEY,              -- 全局单调分配, 永不复用(沿用 SuperMew 原则)
  token TEXT NOT NULL UNIQUE
);
CREATE TABLE bm25_token_stats (
  space_id TEXT NOT NULL,
  vocab_idx INTEGER NOT NULL,
  df INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (space_id, vocab_idx)
);
CREATE TABLE bm25_space_stats (
  space_id TEXT PRIMARY KEY,
  total_docs BIGINT NOT NULL DEFAULT 0,
  sum_token_len DOUBLE PRECISION NOT NULL DEFAULT 0
);
```

- **摄入/删除**：与 parent_chunks 同事务增减 df/N（新 token 先 `INSERT ... ON CONFLICT DO NOTHING RETURNING` 取 idx），文件竞态消失；
- **查询**：按会话空间加载该空间 df/N 计算 query 稀疏向量（IDF 按空间分账，比原全局 IDF 更准）；
- **双存储一致性**：Milvus 与 Postgres 无法跨库原子，沿用 SuperMew 的删除顺序纪律并文档化；新增 `labnote doctor --verify-bm25` 夜间任务，全量比对 Milvus 各空间 chunk 计数与 Postgres 统计，不一致告警并可重建统计；
- **分词器沿用但修两个洞**：中文单字+英文小写词的基础上，**保留数字 token**（原实现丢弃数字，数值型查询零稀疏足迹）；可选引入 bigram 提高中文短语区分度（由评估决定，默认关）。

## 7. 摄入管线升级（链路 A）

入口三种，同一管线：

1. **监视目录**（服务器上 `~/labnote/inbox/<space>/`，轮询）：成员放 PDF/Markdown 自动入库——"自动入知识库"的最小可靠实现，Zotero 定期导出到此目录即联动；
2. **上传 API / 前端**（editor 及以上）；
3. **回流 API**（链路 C，`kind=experiment`）。

管线步骤：

1. **解析**：`.md` 直读（front-matter 进元数据，`paper:` 字段关联论文）；PDF 用 pymupdf4llm 整篇转 markdown（个人/实验室内部使用无 AGPL 顾虑）；表格/公式段落标记 `kind`；解析质量差的页回退页级整块。
2. **结构分块（替换字符按页切分）**：整篇先解析、**跨页建树**；按 markdown 标题层级切 L1（节，≤1200 token）/ L2（小节/段组，≤600）/ L3（段落叶块，300–400 token），保留 SuperMew 三级 auto-merging 机制不变，仅改变切法与预算（CJK 感知 token 近似：1 汉字≈1、4 ASCII≈1）。叶块记录来源页码用于引用。
3. **元数据**：`heading_path`（"3 方法 > 3.2 训练策略"）、`kind`（paper|note|experiment）、authors/year/venue/arxiv（尽力抽取，失败 CLI 可补）。
4. **写入**：叶块向量 + 稀疏向量入 Milvus（partition 按 space 路由），L1/L2 入 parent_chunks，全部元数据入 documents——同一文档重传 = 删除旧 + 写新，事务化（§5-6）。

## 8. 检索与会话

检索主链路**保留 SuperMew 形态**：hybrid（bge-m3 稠密 + BM25 稀疏，Milvus 内 `hybrid_search` + RRFRanker）→ auto-merging（叶块命中 ≥2 合并父块，阈值可配）→ rerank（Jina 兼容 API，可关）→ 空间过滤全程生效。修正与约束：

- **重排顺序修正**（评估门槛）：现状"先重排后合并"导致合并出的父块无重排分、分数语义混排。改为"先合并后重排"：合并后对父块+剩余叶块统一重排，无重排时回退 RRF 序。改动进 S4，以 gold 集对比表决定去留；
- **CRAG 循环保留**（评分→step-back/HyDE 改写→二次检索），但修 §5-4 的模型默认值，且改写路径增加缓存避免重复烧 LLM 调用；
- **降级可见**：hybrid→dense 降级、rerank 失败等全部落 trace 与日志（§5-5）。

会话层：沿用 LangChain agent + SSE + `rag_trace` 落库；会话绑定 (user, 空间集合)；工具集收敛为 `search_knowledge`（隐式空间）、`read_chunk`、`list_documents`；引用格式 `[标题 §heading p.N]` 且 chunk_id 必须真实可解析。

## 9. 评估体系（铁律不变）

任何检索参数/组件增删（rerank 顺序、RRF k、auto-merge 阈值、bigram、CRAG 开关）先出对比表再合入：

- `eval/gold.jsonl`：`{space, query, gold_chunk_ids[], note}`，起步 30 条（实验室真实语料上人工标注），随手持续补；
- 指标：recall@5/@10（any-hit 与全命中）、MRR@10；对话质量另设 20 条固定问题 LLM 评分 + 引用真实性脚本校验（100% 可解析才算过）；
- `labnote eval` 本地 harness 直打检索 API，输出 `fts-only / dense-only / hybrid / hybrid+rerank` 四行对比表，落档 `eval/reports/`；LangSmith 仅作可选的对话级远端评估；
- 验收线：hybrid 的 recall@10 ≥ 两条单路，否则查因再继续。

## 10. 部署与运维（实验室生产）

**拓扑**：单服务器 docker compose，8 个服务 = 原 6（milvus/etcd/minio/postgres/redis/attu）+ `api`（labnote 应用容器，uv + uvicorn）+ `caddy`。应用不再跑宿主机。

- **网络**：仅 Caddy 暴露 443（实验室内部域名或内网 CA 签发证书）；Postgres/Redis/Milvus/etcd/minio 不映射宿主端口，仅 compose 内网；Attu 不挂公网路由，走 SSH 隧道或实验室 VPN 访问；
- **资源**：10 人/万级文档：4C8G 起步、8C16G 舒适；磁盘 ≈ 语料 ×3（原始件+解析产物+索引）；bge-m3 CPU 推理可接受（批量摄入为主，首次拉模型约 2GB）；LLM/rerank 走外部 API（Ark/Jina，key 进 secret 文件）；
- **备份（夜间）**：① pg_dump（含 BM25 统计——JSON 文件已消灭，备份集简化）；② Milvus 卷快照（`milvus-backup` 工具或停写窗口 tar 卷）；③ 原始文档目录；④ 验收必须包含一次**恢复演练**：空机恢复全栈 + doctor 校验通过；
- **监控**：不引入 Prometheus 体系；健康检查（compose healthcheck + `/healthz`）、rag_trace 已有的全链路观测、结构化日志滚动保留即可；
- **不要过度工程**：不上 k8s、不上 Milvus 分布式、不做双机高可用；实验室内部工具，夜备份 + 可恢复 = 合格生产。

## 11. autolab-lite 集成（链路 C，S8，等其就绪）

契约与 v1 相同，叠加用户映射：

1. **轮次种子注入**：autolab `stages/research.py build_task()` 追加"## 知识库相关"节——以 `idea + context.md 目标` 为 query 调 `POST /api/v1/retrieve`（PAT 认证的服务账号，绑定指定空间）取 top5 父块；
2. **Agent 工具**：autolab `agent/tools.py` 加 `search_knowledge(query)` 薄包装；
3. **回流**：`_record_round()` 推送轮次摘要为 `kind=experiment` 文档（owned by 服务账号），使下一轮与对话可检索"上次为什么失败"；
4. 启动参数 `knowledge_space` 进 research start 表单。
前置：`refactor/stage-agents` 合 main 且真实 GLM 跑通研究循环；集成本身 ≤1 天。

## 12. 分阶段路线（学习优先，一步一验收）

执行方式从"按生产风险排序"改为"按数据流排序、每阶段一个 RAG 主题"：前半程 S0–S5 是 RAG 手艺（巩固开发流程、攒调优经验），后半程 S6–S8 是生产工程。评估仪器在 S1 就建好，S2 起任何改动都以 gold 集对比表为门——这套流程本身就是"以后能自己优化和评估这个 RAG"的训练。每阶段结束留三样东西：**可运行的系统、一张 eval 报告、一段踩坑笔记**（笔记进 docs/，逐渐长成你自己的 RAG 手册）。

| 阶段 | 主题 | 学习目标 | 做什么 | 验收 |
|---|---|---|---|---|
| **S0**（半天） | 跑通与观察 | 建立全链路心智图 | SuperMew-main 原地 compose 起全栈；上传 3–5 篇论文、对话，逐条读 rag_trace；对照源码把 解析→分块→嵌入→混合检索→RRF→重排→合并→CRAG 每一环落到 文件:行号 | 产出一张"管线环节 ↔ 代码位置"对照表；不改任何代码 |
| **S1**（3–5 天） | 建仓与评估仪器 | 检索指标、gold 集构建 | 仓库手术（仅无害清理+改名，§3）；修 hit 解析（§5-3——不修则 dense 基线是假的）；写 `labnote eval` harness；标注 30 条 gold | 四行基线表落档（fts/dense/hybrid/hybrid+rerank）；对每行数字能说出"为什么" |
| **S2**（1–1.5 周） | 摄入与分块 | 分块决定检索上限 | §7 全部：pymupdf4llm、结构三级树、markdown 笔记、监视目录 | 新旧分块同 gold 集对比：结构分块 recall@10 ≥ 字符按页；heading_path 带来的引用质量改善有实例 |
| **S3**（2–3 天） | 检索参数 | 各旋钮的敏感度 | 单变量扫：candidate_k / rrf_k / top_k、BM25 k1/b、数字 token 修复、（可选）中文 bigram | 每旋钮一张对比表；选出本库最优默认值写回 .env |
| **S4**（2–3 天） | 重排与合并 | 分数语义与 cross-encoder | "先合并后重排"修正（§8）、auto-merge 阈值扫描 | 对比表决定修正去留；能讲清 RRF 分/重排分/继承分为何不可混排 |
| **S5**（2–3 天） | 查询变换 | 改写的收益与成本 | GRADE_MODEL 修复（§5-4）、改写路径缓存、CRAG 开关对比 | CRAG 开/关两行表 + 每查询 LLM 调用计数对比，数据定默认值 |
| **S6**（1.5–2 周） | 多租户与加固 | 生产工程 | §4 + §6 + §5 其余项（全局状态、注入、静默吞异常、清理链路） | 权限矩阵测试；10 并发上传后 doctor 校验一致；双用户并发对话无串台 |
| **S7**（2–3 天） | 部署 | 运维 | §10 全部 | HTTPS；Attu 仅隧道可达；夜备份 + 一次完整恢复演练 |
| **S8**（≤1 天） | autolab 集成 | — | §11 | 研究会话出现"知识库相关"节；`search_knowledge` 可见；回流可检索 |

节奏与硬约束：

- **每阶段可独立停**。S2 结束你就有一个分块质量优于原版的个人知识库，随时可用、可用很久；S6 之前它就是单用户系统，这不影响任何 RAG 学习目标。
- **多租户后置的代价很小**：S6 本来就要新建带 partition key 的 collection（冷启动），摄入与检索届时补一个 `space_id` 参数即可；换来的是 S2–S5 期间不用背着权限模型调 RAG。
- **§5 修复按阶段分布**：hit 解析在 S1（评估正确性依赖它），GRADE_MODEL 在 S5，其余集中在 S6（进多用户之前修完即可）。
- **不要跳过 S0/S1 直接改代码**——没有仪器之前，一切"感觉变好了"都不算数。
- gold 集是唯一贯穿全程的资产，标注质量 > 数量，日常使用中随手补。

与 v2 初稿 L 计划的对应：L0→S0+S1，L1→S6，L2→S1+S2，L3→S7，L4→拆入 S2–S5，L5→S8。

## 13. 决策对照（v1 → v2，及教训来源）

| 决策项 | v1（个人版） | v2（实验室生产版） | 依据 |
|---|---|---|---|
| 基座 | 从零写，SQLite 单文件 | SuperMew backend/ 全栈保留 | 有服务器、多用户、生产诉求；重写存储层的收益消失 |
| 容器 | 0（SQLite+FTS5+sqlite-vec） | 8（原 6 + api + caddy） | 同上；实验室规模 Milvus standalone 恰当 |
| 稀疏检索 | FTS5 内建 BM25 | Milvus 稀疏向量（保留），状态从 JSON 文件入 Postgres 按空间分账 | 基座既定；文件竞态是生产阻断（SuperMew 教训），IDF 按空间更准 |
| 融合/重排 | RRF、先合并后重排 | 同；重排顺序修正作为 L4 评估门控项 | advanced-rag 分数混排教训 + SuperMew 重排语义混乱教训 |
| 分块 | 标题层级 + token 预算（两级） | 同思路，落到三级树以保留 auto-merging | SuperMew 字符按页切分教训 + advanced-rag ReportChunker 正面经验 |
| 多租户 | 无（单用户） | users/spaces/members + partition key + 全链路过滤 | 生产必需；SuperMew 单租户缺口。空间 API 形态/默认空间/名称去重借自 advanced-rag 的 knowledge_spaces；成员与可见性为其所无；隔离用 Milvus partition key 而非其 collection-per-space（跨空间单查 + 单索引，见 §4.2） |
| 框架 | 无框架手写循环 | 沿用基座的 LangChain/LangGraph | 基座既定，框架已验证可用；v1 的家规顾虑仅限 autolab 仓内 |
| 查询变换 | M3 评估后决定 | CRAG 保留（基座已有），修模型默认值 + 加缓存 | 有效性仍由 gold 集把关 |
| 评估 | 先行铁律 | 不变，harness 本地化 | 两项目共同最大教训 |
| 部署 | 笔记本常驻 | 服务器 compose + TLS + 备份/恢复演练 | 生产目标 |

## 14. 开放问题

1. 空间配额/上传限额是否第一版就要（防个别人灌满磁盘）——建议 L1 只做全局上限，按空间配额后补；
2. 嵌入模型本地 bge-m3（现状）还是 API 化以省服务器内存——L3 部署时按服务器实测定；
3. 实验室成员的"个人空间"是否默认创建——建议默认每人一个私有空间 + admin 建共享空间；
4. autolab 服务账号的检索范围是否限定单空间——建议限定（研究项目绑定一个空间，避免跨空间噪声）。
