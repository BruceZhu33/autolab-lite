# LABNOTE（autolab-kb）项目总览——给 autolab-lite 侧的说明书

日期：2026-10-16 ｜ 版本：S8 契约版（v0.4-s8-contract） ｜ 仓库：`autolab-kb`（Mac 本地 + 服务器 `~/labnote`）

---

## 一、这是什么

LABNOTE 是一个**多用户科研知识库**——论文 PDF + 实验记录进去，语义检索 + Agent 对话出来，autolab 研究循环可以往里查（种子注入/工具检索）也可以往里写（实验结论回流）。它按总纲（`autolab-lite/docs/KNOWLEDGE-BASE-DESIGN-2026-10-13.md`）的 S0–S8 路线建成，基座是 SuperMew-main（一个 LangChain v1 + Milvus 的教学级 RAG），经 S1–S8 渐进手术成生产系统。

**与 autolab-lite 的关系（§2 决策）**：姊妹服务，非内嵌。HTTP 契约对接，无 import 依赖。

```
实验室成员 ──HTTPS──▶ labnote（论文库 / 笔记 / 对话）
                        │  PUT /sessions/{id}/spaces（会话绑空间）
autolab-lite ──PAT──▶ ──┤  POST /api/v1/retrieve（种子注入 / search_knowledge 工具）
（链路 C）              └─ POST /api/v1/ingest（每轮实验结论回流，下一轮可检索失败原因）
```

## 二、系统形态（服务器 192.0.2.46）

- **入口**：`https://192.0.2.46`（Caddy 内网自签 TLS）→ api 容器（uvicorn）
- **栈**：docker compose 8 服务（caddy / labnote-api / postgres / redis / milvus-standalone + etcd + minio / attu），数据面端口全部收回 `127.0.0.1`
- **运维**：`/healthz` 探活；夜备份 cron 03:00（pg_dump + Milvus 卷 tar + 文档目录，7 份轮转，70G 磁盘熔断）；恢复演练已通过；`doctor.py` 三方对账
- **模型**：嵌入 bge-m3（GPU）+ 本地重排 bge-reranker-base（GPU）+ LLM glm-4.5-flash（智谱免费档，OpenAI 兼容）

## 三、检索质量（38 条 gold，含 8 条数字查询）

| 指标 | 基线(S1) | 终值(S6 多租产后) | 来源 |
|---|---|---|---|
| any@5 | 0.833 | **0.921** | 结构分块 +8pp → 数字token +2.6 → 本地重排 |
| any@10 | 0.867 | **0.947** | 同上 |
| all@10 | 0.600 | **0.868** | 结构分块贡献最大（跨页树 +26.8pp 总账） |
| MRR@10 | 0.612 | **0.714** | 本地重排主贡献 |

每步有对比表（`eval/reports/` 6 份），被否决的改动也留档（§8"先合并后重排"实测更差未转正）。多租户改造经回归门验证零回退。

## 四、多租户模型（S6）

- **空间**：`spaces` 表，shared（全员 reader+）/ private（仅成员）；默认空间 `lab-shared` 兜底
- **角色**：owner > editor > reader（上传需 editor、成员管理需 owner、检索对话 reader 即可）
- **隔离**：Milvus collection 以 `space_id` 为 partition key；BM25 IDF 按空间分账（PG 三表）；会话绑定检索域（`metadata_json.space_ids`，LLM 无权选择范围——防提示注入越权）
- **实测**：双用户并发、跨空间零泄漏、doctor 三方（Milvus/账本/documents）对账一致

## 五、对 autolab-lite 的三个接口（§11，已上线可调）

认证：`Authorization: Bearer <PAT>`（服务账号 autolab-svc，PAT 在服务器 `~/labnote/.env` 的 `PAT_AUTOLAB_TOKEN`）。

```bash
# 种子注入 / search_knowledge 工具：query → top_k 父块（含 heading_path/kind/score）
curl -k -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{"query":"EGDK 低标注预算策略","top_k":5}' \
  https://192.0.2.46/api/v1/retrieve

# 实验结论回流：markdown 文本 → 三级树入库，下一轮可检索
curl -k -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{"filename":"round3.md","text":"# 结论\n...","kind":"experiment"}' \
  https://192.0.2.46/api/v1/ingest
```

默认落在专属 `autolab` 空间（§14.4：研究项目绑单空间，避免跨空间噪声）。**贴入式补丁**（三处改动：工具/种子注入/回流）已备好在 `docs/S8-AUTOLAB-PATCH.md`，等 `refactor/stage-agents` 合并后照贴。全链路已用 `scripts/s8_demo.py` 实测通过。

## 六、关键设计决策（为什么长这样）

| 决策 | 选择 | 依据 |
|---|---|---|
| 基座 | SuperMew backend 全栈保留 + 定向手术 | 重写无收益；检索主链（三级树+hybrid+RRF+auto-merge）是核心资产 |
| 空间隔离 | 单 collection + partition key | 跨空间一次 hybrid_search、单索引单备份（advanced-rag 的 collection-per-space 需扇出拼接） |
| 分块 | pymupdf4llm 跨页三级树（L1≤1200tok/L2≤600/L3≤400，贪心装箱双约束） | 对比表 +26.8pp all@10；嵌套包含保 auto-merge |
| 重排 | 本地 CrossEncoder（A6000） | +10.5pp all@10 零外部依赖；Jina 代码就绪未配 |
| BM25 | 词表全局单调永不复用 + df/N 按空间入 PG | 换维度=全量重嵌入；账本进备份集 |
| CRAG | 保持开启（+2 LLM 调用/查询） | 开/关 parity 实证；救援路径价值待 gold v3 难查询验证 |

## 七、还在 TODO

- autolab 侧贴补丁（等合并，≤1 天）｜ 前端空间切换器 ｜ 正式 Dockerfile（现用 venv 挂载法）｜ watch_inbox 入 systemd ｜ Jina 后端补测 ｜ gold v3 补难查询重测 CRAG ｜ 服务器磁盘水位监控（S7 清单项）

## 八、文档地图

- 设计总纲：`autolab-lite/docs/KNOWLEDGE-BASE-DESIGN-2026-10-13.md`（v2）
- 代码走读（S0）：`SuperMew-main/S0-CODE-WALKTHROUGH.md`（全链 file:line 对照）
- 阶段总结：`docs/S1-S5-SUMMARY.md`；踩坑笔记：`docs/S1-PITFALLS-2026-10-15.md`（22 条）
- 部署 SOP：`docs/S7-DEPLOY.md`；autolab 补丁：`docs/S8-AUTOLAB-PATCH.md`
- 评估报告：`eval/reports/`（s2 分块对比 / s3 参数扫描 / s4 重排矩阵 / s5 查询变换 / 回归门）
