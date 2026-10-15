# S1 收尾文档：两仓库对照 × 接入工作台方案

日期：2026-10-15
输入：S0 部署与走读成果（SuperMew 全码走读 + 服务器实测）、KNOWLEDGE-BASE-DESIGN-2026-10-13 v2、advanced-rag-main 读码确认
用途：S1（建仓与评估仪器）开工前的决策快照

---

## 一、两仓库结构对照（模块级，已读码确认）

| 维度 | SuperMew-main（基座） | advanced-rag-main（参照源） | LABNOTE 取向 |
|---|---|---|---|
| 定位 | 教学级完整 RAG 全链路 | 匿名生产级 AI 助手系统（深度研究产品） | 基座 = SuperMew，生产化改造 |
| 后端 | FastAPI 单体 15 文件 | FastAPI 分层工程（routers/services/agents/parsers/chunking/retrieval/eval） | 基座形态保留，S2 起按需加层 |
| 前端 | vanilla JS 静态页 | Next.js web 应用 | 基座前端 + 登录页 + 空间切换（§3） |
| 存储 | Milvus+etcd+minio / PG / Redis / bm25_state.json | Mongo / Qdrant / Neo4j / Redis | Milvus+PG+Redis；BM25 账本入 PG（§6） |
| 嵌入 | bge-m3 本地单模型（部署级锁定） | Ollama 多模型运行时可选（embedding_model 参数） | bge-m3 锁定（§4.2 已论证约束可接受） |
| 解析 | PyPDF/Docx2txt/UnstructuredExcel | 多 parser 工厂 + OCR(Paddle) + markdown parser | S2 换 pymupdf4llm→markdown（§7） |
| 分块 | 字符按页三级嵌套树（RecursiveSplitter×3） | 分块器族：simple/sliding/hybrid(语义)/Report(结构化)，jieba 分词 | **借 ReportChunker 思想**：标题层级 + token 预算，落到三级树（S2） |
| 检索融合 | Milvus hybrid(dense+BM25sparse) + RRF(名次) | 三路(向量+关键词+图谱) + 加权分数 merge | 保 RRF 名次融合；**不借加权分数 merge**（量纲混排教训） |
| 重排 | Jina API（失败降级截断） | 本地 CrossEncoder bge-reranker-base | **借本地重排选项**——服务器有 A6000，S4 时作为 Jina 替代实测 |
| 上下文扩展 | auto-merge 三级换父块 | 邻居扩展（命中块拉前后窗口） | 保 auto-merge（三级树配套） |
| 查询变换 | CRAG 图（grade→step_back/hyde/complex→重搜） | query_analyzer / query_understanding_service | 保 CRAG，S5 修默认值 + 加缓存 |
| Agent | LangChain v1 create_agent + LangGraph（单 agent 两工具） | 自建多代理（coordinator/workflow/experts/deep-research） | 基座单 agent 起步；工作台多代理是后话 |
| 多租户 | admin/user 两级，文档管理=admin | **知识空间**（匿名、无用户、无成员概念） | 借 API 形态 + is_default 兜底 + casefold 去重 + kb_命名；成员/可见性为新增（§4） |
| 空间隔离 | 无 | 一空间一 Qdrant collection + asyncio.gather 扇出拼接 | **改用 Milvus 单 collection + partition key**（§4.2：跨空间单查、单索引） |
| 评估 | 无 | eval/（retrieval_eval.py + dataset + 样例） | S1 建 labnote eval harness，形态参考它 |
| 运行时开关 | 无 | runtime_config（rerank/图谱 热开关） | S3/S5 扫参数时值得借（不改代码切模式） |

## 二、从 advanced-rag-main 借的四样、不借的四样

**借（已读码核实）**：
1. `routers/knowledge_spaces.py` 的空间 API 形态：分页列表、`name_key=casefold` 去重撞名 409、`kb_<slug>_<uuid8>` 稳定命名、`is_default` 默认空间兜底（62-108 行确认）。
2. `chunking/report_chunker.py` 的结构化分块：四种标题正则（markdown/中文章节/数字编号/中文编号）→ `section_path` 路径维护 + token 预算软硬限 + 超大块句级粗切（12-21、93-138 行）。S2 把它改造成三级树版：L1=节、L2=小节、L3=段落。
3. 本地 CrossEncoder 重排（rag_retriever.py `_get_reranker`：延迟加载、失败自动禁用不反复重试）——A6000 上跑 bge-reranker-base 零外部依赖，S4 与 Jina API 对比后择优。
4. 动态 k 裁剪思想（`_dynamic_k_from_scores`：重排分数区分度自适应 k）——S3 扫参数时参考。

**不借（理由在设计文档已定）**：collection-per-space（改 partition key）、Neo4j 图谱检索（非目标）、Mongo/Qdrant 技术栈（基座已定）、匿名系统模型（LABNOTE 多用户）。

## 三、S0 的增量发现（并入设计文档 §5 的 P0 清单）

静态分析没抓到、S0 实测炸出来的三项：

| # | 发现 | 位置 | 建议 |
|---|---|---|---|
| A | **closed-channel 重连失效**：`_is_closed_channel_error` 只认 `ValueError`，pymilvus 实抛 `MilvusException`（code=1 包装）→ 重连永不触发，对话后通道即中毒，全进程报废 | milvus_client.py:34-36 | **提前到 S1 修**（eval harness 高频打检索，必踩）；一行改消息匹配 |
| B | GRADE_MODEL 未配置时评分节点静默走 rewrite（trace 脏） | rag_pipeline.py:16,161-169 | .env 先配对（已做：智谱 glm-4.5-flash），S5 修默认值 |
| C | minio/minio 镜像已从全部公共 registry 撤销（MinIO 2025 起只发源码） | docker-compose.yml | S1 建仓时正式化：compose 直接改 `alpine/minio` + `entrypoint: []` + `user: root`（服务器已用 override 验证过行为等价），不留隐式 override 依赖 |

S0 遗产可复用：服务器 192.0.2.46 ~/SuperMew（66 向量参考环境、智谱免费档 key、bge-m3@cuda、管理员邀请码机制已验证）、S0-CODE-WALKTHROUGH.md（含全部 file:line 与坑清单）。

## 四、S1 执行清单（对照 §12：3-5 天，验收 = 四行基线表落档）

**Day 1：仓库手术（§3，仅无害操作）**
- [ ] `git init` /Users/juan/Documents/autolab/autolab-kb；从 SuperMew 复制 backend/ frontend/ docker-compose.yml pyproject.toml uv.lock .python-version
- [ ] 删除 langchain-study/、app/、test_embedding.py；volumes/ 进 .gitignore（绝不入库）
- [ ] 包名 supermew→labnote；.env.example 增补 JWT_SECRET / 模型路径 / 空间配额占位；minio 正式化（发现 C）
- [ ] 首次提交 = 纯复制可运行态（不混改动）；服务器 clone 起栈验证（.env 抄 S0 验证过的组合）

**Day 2：两处定向修复**
- [ ] §5-3 hit 解析一致：milvus_client.py:263-274（hybrid 路径）与 310-318（dense 路径）统一双形态 `_format_hit`（app/ 复现代码里有修好的版本可对照移植）
- [ ] S0-A closed-channel：`isinstance(exc, ValueError)` → `"closed channel" in str(exc).lower()`
- [ ] 各加一个单测/冒烟脚本：构造 hybrid 失败降级 dense 字段非空；杀通道后下一次调用自愈

**Day 3-4：评估仪器（§9）**
- [ ] `labnote eval` harness：直打检索 API，四模式开关（fts-only=稀疏单路 / dense-only / hybrid / hybrid+rerank——rerank 需临时配 Jina key 或先以 rerank-off 出三行）
- [ ] gold 集 30 条：上传 5-8 篇真实论文 + 笔记到参考环境，人工标 `{query, gold_chunk_ids[], note}`
- [ ] 指标 recall@5/@10（any-hit 与全命中）、MRR@10；报告落 eval/reports/（含环境指纹：模型、参数、日期）
- [ ] 验收问答：对表中每个数字能说出"为什么"（如 dense < fts 的查询长什么样）

**Day 5：缓冲与归档**
- [ ] 踩坑笔记一篇进 docs/（S0 部署实录可直接改造：Clash TUN、镜像断供、模型分发、Ark/智谱账号坑）
- [ ] 回主会话对齐 S2（摄入与分块）开工口径

## 五、待决策项（2026-10-15 已全部拍板）

1. **closed-channel 修进 S1** ✅（S1-D2 执行）
2. **minio 正式化方式：直接改 compose 原文** ✅（已在 S1-D1 落地：alpine/minio + entrypoint [] + user root，原因与误报说明写进 compose 注释）
3. **rerank 双后端都做** ✅（S1 harness 做成可切换：Jina API + 本地 CrossEncoder，S4 用 gold 集数据对比后定默认）

## 六、后续路线决策（2026-10-15 追加）

用户确认走 **路线 C：先夯实再重构**——S2-S3 在 autolab-kb 上完成检索质量升级（结构化分块、参数调优，eval 把关），之后增设一个"结构重构"阶段：将平铺 backend 重排为 advanced-rag 式分层（routers/services/chunking/retrieval/eval）并引入知识空间（借其 API 形态 + Milvus partition key 隔离）。重构以 v1+ 基线为安全网。S1 三件套（系统/eval 报告/踩坑笔记）已落，见 autolab-kb docs/S1-PITFALLS。
